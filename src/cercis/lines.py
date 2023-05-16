import itertools
import math
import re
import sys
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from blib2to3.pgen2 import token
from blib2to3.pytree import Leaf, Node
from cercis.brackets import COMMA_PRIORITY, DOT_PRIORITY, BracketTracker
from cercis.indent import Indent, MultipleIndents
from cercis.mode import Mode, Preview
from cercis.nodes import (
    BRACKETS,
    CLOSING_BRACKETS,
    OPENING_BRACKETS,
    STANDALONE_COMMENT,
    TEST_DESCENDANTS,
    child_towards,
    is_import,
    is_multiline_string,
    is_one_sequence_between,
    is_type_comment,
    is_with_or_async_with_stmt,
    replace_child,
    syms,
    whitespace,
)
from cercis.strings import str_width

# types
T = TypeVar("T")
Index = int
LeafID = int
LN = Union[Leaf, Node]

# This regex should contain a single capture group capturing the entire match.
_PRAGMA_REGEX = re.compile("( *# (?:pylint|pytype|noqa|type(?=: *ignore)):)")


@dataclass
class Line:
    """Holds leaves and comments. Can be printed with `str(line)`."""

    mode: Mode
    depth: Tuple[Indent, ...] = field(default_factory=tuple)
    leaves: List[Leaf] = field(default_factory=list)
    # keys ordered like `leaves`
    comments: Dict[LeafID, List[Leaf]] = field(default_factory=dict)
    bracket_tracker: BracketTracker = field(default_factory=BracketTracker)
    inside_brackets: bool = False
    should_split_rhs: bool = False
    magic_trailing_comma: Optional[Leaf] = None

    def render_indent_chars(self, for_width_calculation: bool = False) -> str:
        return self.all_indents.render(for_width_calculation)

    def calc_total_indent_width(self) -> int:
        return self.all_indents.calc_total_width()

    def append(
            self, leaf: Leaf, preformatted: bool = False, track_bracket: bool = False
    ) -> None:
        """Add a new `leaf` to the end of the line.

        Unless `preformatted` is True, the `leaf` will receive a new consistent
        whitespace prefix and metadata applied by :class:`BracketTracker`.
        Trailing commas are maybe removed, unpacked for loop variables are
        demoted from being delimiters.

        Inline comments are put aside.
        """
        has_value = leaf.type in BRACKETS or bool(leaf.value.strip())
        if not has_value:
            return

        if token.COLON == leaf.type and self.is_class_paren_empty:
            del self.leaves[-2:]
        if self.leaves and not preformatted:
            # Note: at this point leaf.prefix should be empty except for
            # imports, for which we only preserve newlines.
            leaf.prefix += whitespace(
                leaf, complex_subscript=self.is_complex_subscript(leaf)
            )
        if self.inside_brackets or not preformatted or track_bracket:
            self.bracket_tracker.mark(leaf)
            if self.mode.magic_trailing_comma:
                if self.has_magic_trailing_comma(leaf):
                    self.magic_trailing_comma = leaf
            elif self.has_magic_trailing_comma(leaf, ensure_removable=True):
                self.remove_trailing_comma()
        if not self.append_comment(leaf):
            self.leaves.append(leaf)

    def append_safe(self, leaf: Leaf, preformatted: bool = False) -> None:
        """Like :func:`append()` but disallow invalid standalone comment structure.

        Raises ValueError when any `leaf` is appended after a standalone comment
        or when a standalone comment is not the first leaf on the line.
        """
        if self.bracket_tracker.depth == 0:
            if self.is_comment:
                raise ValueError("cannot append to standalone comments")

            if self.leaves and leaf.type == STANDALONE_COMMENT:
                raise ValueError(
                    "cannot append standalone comments to a populated line"
                )

        self.append(leaf, preformatted=preformatted)

    @property
    def all_indents(self) -> MultipleIndents:
        """All the indentations of this line"""
        return MultipleIndents(indents=self.depth, mode=self.mode)

    @property
    def is_comment(self) -> bool:
        """Is this line a standalone comment?"""
        return len(self.leaves) == 1 and self.leaves[0].type == STANDALONE_COMMENT

    @property
    def is_decorator(self) -> bool:
        """Is this line a decorator?"""
        return bool(self) and self.leaves[0].type == token.AT

    @property
    def is_import(self) -> bool:
        """Is this an import line?"""
        return bool(self) and is_import(self.leaves[0])

    @property
    def is_with_or_async_with_stmt(self) -> bool:
        """Is this a with_stmt line?"""
        return bool(self) and is_with_or_async_with_stmt(self.leaves[0])

    @property
    def is_class(self) -> bool:
        """Is this line a class definition?"""
        return (
            bool(self)
            and self.leaves[0].type == token.NAME
            and self.leaves[0].value == "class"
        )

    @property
    def is_stub_class(self) -> bool:
        """Is this line a class definition with a body consisting only of "..."?"""
        return self.is_class and self.leaves[-3:] == [
            Leaf(token.DOT, ".") for _ in range(3)
        ]

    @property
    def is_def(self) -> bool:
        """Is this a function definition? (Also returns True for async defs.)"""
        try:
            first_leaf = self.leaves[0]
        except IndexError:
            return False

        try:
            second_leaf: Optional[Leaf] = self.leaves[1]
        except IndexError:
            second_leaf = None
        return (first_leaf.type == token.NAME and first_leaf.value == "def") or (
            first_leaf.type == token.ASYNC
            and second_leaf is not None
            and second_leaf.type == token.NAME
            and second_leaf.value == "def"
        )

    @property
    def is_class_paren_empty(self) -> bool:
        """Is this a class with no base classes but using parentheses?

        Those are unnecessary and should be removed.
        """
        return (
            bool(self)
            and len(self.leaves) == 4
            and self.is_class
            and self.leaves[2].type == token.LPAR
            and self.leaves[2].value == "("
            and self.leaves[3].type == token.RPAR
            and self.leaves[3].value == ")"
        )

    @property
    def is_triple_quoted_string(self) -> bool:
        """Is the line a triple quoted string?"""
        return (
            bool(self)
            and self.leaves[0].type == token.STRING
            and self.leaves[0].value.startswith(('"""', "'''"))
        )

    @property
    def opens_block(self) -> bool:
        """Does this line open a new level of indentation."""
        if len(self.leaves) == 0:
            return False
        return self.leaves[-1].type == token.COLON

    def is_fmt_pass_converted(
            self, *, first_leaf_matches: Optional[Callable[[Leaf], bool]] = None
    ) -> bool:
        """Is this line converted from fmt off/skip code?

        If first_leaf_matches is not None, it only returns True if the first
        leaf of converted code matches.
        """
        if len(self.leaves) != 1:
            return False
        leaf = self.leaves[0]
        if (
            leaf.type != STANDALONE_COMMENT
            or leaf.fmt_pass_converted_first_leaf is None
        ):
            return False
        return first_leaf_matches is None or first_leaf_matches(
            leaf.fmt_pass_converted_first_leaf
        )

    def contains_standalone_comments(self, depth_limit: int = sys.maxsize) -> bool:
        """If so, needs to be split before emitting."""
        for leaf in self.leaves:
            if leaf.type == STANDALONE_COMMENT and leaf.bracket_depth <= depth_limit:
                return True

        return False

    def contains_uncollapsable_type_comments(self) -> bool:
        if not self.mode.wrap_pragma_comments:
            # We bypass this method entirely because we are handling
            # all pragma comments (such as "noqa", "pylint", "type: ignore")
            # together elsewhere.
            return False

        # The following is Black's default (wrap_pragma_comment == True).
        # With this style, if a line has "# type: ignore" as the comment,
        # it will not be wrapped no matter how long it is.

        ignored_ids = set()
        try:
            last_leaf = self.leaves[-1]
            ignored_ids.add(id(last_leaf))
            if last_leaf.type == token.COMMA or (
                last_leaf.type == token.RPAR and not last_leaf.value
            ):
                # When trailing commas or optional parens are inserted by Black for
                # consistency, comments after the previous last element are not moved
                # (they don't have to, rendering will still be correct).  So we ignore
                # trailing commas and invisible.
                last_leaf = self.leaves[-2]
                ignored_ids.add(id(last_leaf))
        except IndexError:
            return False

        # A type comment is uncollapsable if it is attached to a leaf
        # that isn't at the end of the line (since that could cause it
        # to get associated to a different argument) or if there are
        # comments before it (since that could cause it to get hidden
        # behind a comment.
        comment_seen = False
        for leaf_id, comments in self.comments.items():
            for comment in comments:
                if is_type_comment(comment):
                    if comment_seen or (
                        not is_type_comment(comment, " ignore")
                        and leaf_id not in ignored_ids
                    ):
                        return True

                comment_seen = True

        return False

    def contains_unsplittable_type_ignore(self) -> bool:
        if not self.mode.wrap_pragma_comments:
            # We bypass this method entirely because we are handling
            # all pragma comments (such as "noqa", "pylint", "type: ignore")
            # together elsewhere.
            return False

        # The following is Black's default (wrap_pragma_comment == True).
        # With this style, if a line has "# type: ignore" as the comment,
        # it will not be wrapped no matter how long it is.

        if not self.leaves:
            return False

        # If a 'type: ignore' is attached to the end of a line, we
        # can't split the line, because we can't know which of the
        # subexpressions the ignore was meant to apply to.
        #
        # We only want this to apply to actual physical lines from the
        # original source, though: we don't want the presence of a
        # 'type: ignore' at the end of a multiline expression to
        # justify pushing it all onto one line. Thus we
        # (unfortunately) need to check the actual source lines and
        # only report an unsplittable 'type: ignore' if this line was
        # one line in the original code.

        # Grab the first and last line numbers, skipping generated leaves
        first_line = next((leaf.lineno for leaf in self.leaves if leaf.lineno != 0), 0)
        last_line = next(
            (leaf.lineno for leaf in reversed(self.leaves) if leaf.lineno != 0), 0
        )

        if first_line == last_line:
            # We look at the last two leaves since a comma or an
            # invisible paren could have been added at the end of the
            # line.
            for node in self.leaves[-2:]:
                for comment in self.comments.get(id(node), []):
                    if is_type_comment(comment, " ignore"):
                        return True

        return False

    def trailing_pragma_comment_length(self) -> int:
        """
        This method comes from the wonderful implementation in Pyink:
        https://github.com/google/pyink/blob/f93771c02e9a26ce9508c59d69c9337c95797eac/src/pyink/lines.py#L316-L328

        Pyink is another fork from Black, and inherits Black's MIT license.
        """
        if not self.leaves:
            return 0

        last_leaf = self.leaves[-1]
        length = 0
        for comment in self.comments.get(id(last_leaf), []):
            # str(comment) contains the whitespace preceding the `#`
            comment_str = str(comment)
            parts = _PRAGMA_REGEX.split(comment_str, maxsplit=1)
            if len(parts) == 3:
                length += len(parts[1]) + len(parts[2])
        return length

    def contains_multiline_strings(self) -> bool:
        return any(is_multiline_string(leaf) for leaf in self.leaves)

    def has_magic_trailing_comma(
            self, closing: Leaf, ensure_removable: bool = False
    ) -> bool:
        """Return True if we have a magic trailing comma, that is when:
        - there's a trailing comma here
        - it's not a one-tuple
        - it's not a single-element subscript
        Additionally, if ensure_removable:
        - it's not from square bracket indexing
        (specifically, single-element square bracket indexing)
        """
        if not (
            closing.type in CLOSING_BRACKETS
            and self.leaves
            and self.leaves[-1].type == token.COMMA
        ):
            return False

        if closing.type == token.RBRACE:
            return True

        if closing.type == token.RSQB:
            if (
                closing.parent
                and closing.parent.type == syms.trailer
                and closing.opening_bracket
                and is_one_sequence_between(
                    closing.opening_bracket,
                    closing,
                    self.leaves,
                    brackets=(token.LSQB, token.RSQB),
                )
            ):
                return False

            if not ensure_removable:
                return True

            comma = self.leaves[-1]
            if comma.parent is None:
                return False
            return (
                comma.parent.type != syms.subscriptlist
                or closing.opening_bracket is None
                or not is_one_sequence_between(
                    closing.opening_bracket,
                    closing,
                    self.leaves,
                    brackets=(token.LSQB, token.RSQB),
                )
            )

        if self.is_import:
            return True

        if closing.opening_bracket is not None and not is_one_sequence_between(
            closing.opening_bracket, closing, self.leaves
        ):
            return True

        return False

    def append_comment(self, comment: Leaf) -> bool:
        """Add an inline or standalone comment to the line."""
        if (
            comment.type == STANDALONE_COMMENT
            and self.bracket_tracker.any_open_brackets()
        ):
            comment.prefix = ""
            return False

        if comment.type != token.COMMENT:
            return False

        if not self.leaves:
            comment.type = STANDALONE_COMMENT
            comment.prefix = ""
            return False

        last_leaf = self.leaves[-1]
        if (
            last_leaf.type == token.RPAR
            and not last_leaf.value
            and last_leaf.parent
            and len(list(last_leaf.parent.leaves())) <= 3
            and not is_type_comment(comment)
        ):
            # Comments on an optional parens wrapping a single leaf should belong to
            # the wrapped node except if it's a type comment. Pinning the comment like
            # this avoids unstable formatting caused by comment migration.
            if len(self.leaves) < 2:
                comment.type = STANDALONE_COMMENT
                comment.prefix = ""
                return False

            last_leaf = self.leaves[-2]
        self.comments.setdefault(id(last_leaf), []).append(comment)
        return True

    def comments_after(self, leaf: Leaf) -> List[Leaf]:
        """Generate comments that should appear directly after `leaf`."""
        return self.comments.get(id(leaf), [])

    def remove_trailing_comma(self) -> None:
        """Remove the trailing comma and moves the comments attached to it."""
        trailing_comma = self.leaves.pop()
        trailing_comma_comments = self.comments.pop(id(trailing_comma), [])
        self.comments.setdefault(id(self.leaves[-1]), []).extend(
            trailing_comma_comments
        )

    def is_complex_subscript(self, leaf: Leaf) -> bool:
        """Return True iff `leaf` is part of a slice with non-trivial exprs."""
        open_lsqb = self.bracket_tracker.get_open_lsqb()
        if open_lsqb is None:
            return False

        subscript_start = open_lsqb.next_sibling

        if isinstance(subscript_start, Node):
            if subscript_start.type == syms.listmaker:
                return False

            if subscript_start.type == syms.subscriptlist:
                subscript_start = child_towards(subscript_start, leaf)
        return subscript_start is not None and any(
            n.type in TEST_DESCENDANTS for n in subscript_start.pre_order()
        )

    def enumerate_with_length(
            self, reversed: bool = False
    ) -> Iterator[Tuple[Index, Leaf, int]]:
        """Return an enumeration of leaves with their length.

        Stops prematurely on multiline strings and standalone comments.
        """
        op = cast(
            Callable[[Sequence[Leaf]], Iterator[Tuple[Index, Leaf]]],
            enumerate_reversed if reversed else enumerate,
        )
        for index, leaf in op(self.leaves):
            length = len(leaf.prefix) + len(leaf.value)
            if "\n" in leaf.value:
                return  # Multiline strings, we can't continue.

            for comment in self.comments_after(leaf):
                length += len(comment.value)

            yield index, leaf, length

    def clone(self) -> "Line":
        return Line(
            mode=self.mode,
            depth=self.depth,
            inside_brackets=self.inside_brackets,
            should_split_rhs=self.should_split_rhs,
            magic_trailing_comma=self.magic_trailing_comma,
        )

    def render_as_str_for_width_calculation(self) -> str:
        return self._str_inner(for_width_calculation=True)

    def __str__(self) -> str:
        """Render the line."""
        return self._str_inner(for_width_calculation=False)

    def _str_inner(self, for_width_calculation: bool = False) -> str:
        if not self:
            return "\n"

        indent: str = self.render_indent_chars(for_width_calculation)
        leaves = iter(self.leaves)
        first = next(leaves)
        res = f"{first.prefix}{indent}{first.value}"
        for leaf in leaves:
            res += str(leaf)
        for comment in itertools.chain.from_iterable(self.comments.values()):
            res += str(comment)

        return res + "\n"

    def __bool__(self) -> bool:
        """Return True if the line has leaves or comments."""
        return bool(self.leaves or self.comments)


@dataclass
class RHSResult:
    """Intermediate split result from a right hand split."""

    head: Line
    body: Line
    tail: Line
    opening_bracket: Leaf
    closing_bracket: Leaf


@dataclass
class LinesBlock:
    """Class that holds information about a block of formatted lines.

    This is introduced so that the EmptyLineTracker can look behind the standalone
    comments and adjust their empty lines for class or def lines.
    """

    mode: Mode
    previous_block: Optional["LinesBlock"]
    original_line: Line
    before: int = 0
    content_lines: List[str] = field(default_factory=list)
    after: int = 0

    def all_lines(self) -> List[str]:
        empty_line = str(Line(mode=self.mode))
        return (
            [empty_line * self.before] + self.content_lines + [empty_line * self.after]
        )


@dataclass
class EmptyLineTracker:
    """Provides a stateful method that returns the number of potential extra
    empty lines needed before and after the currently processed line.

    Note: this tracker works on lines that haven't been split yet.  It assumes
    the prefix of the first leaf consists of optional newlines.  Those newlines
    are consumed by `maybe_empty_lines()` and included in the computation.
    """

    mode: Mode
    previous_line: Optional[Line] = None
    previous_block: Optional[LinesBlock] = None
    previous_defs: List[Line] = field(default_factory=list)
    semantic_leading_comment: Optional[LinesBlock] = None

    def maybe_empty_lines(self, current_line: Line) -> LinesBlock:
        """Return the number of extra empty lines before and after the `current_line`.

        This is for separating `def`, `async def` and `class` with extra empty
        lines (two on module-level).
        """
        before, after = self._maybe_empty_lines(current_line)
        previous_after = self.previous_block.after if self.previous_block else 0
        before = (
            # Black should not insert empty lines at the beginning
            # of the file
            0
            if self.previous_line is None
            else before - previous_after
        )
        block = LinesBlock(
            mode=self.mode,
            previous_block=self.previous_block,
            original_line=current_line,
            before=before,
            after=after,
        )

        # Maintain the semantic_leading_comment state.
        if current_line.is_comment:
            if self.previous_line is None or (
                not self.previous_line.is_decorator
                # `or before` means this comment already has an empty line before
                and (not self.previous_line.is_comment or before)
                and (self.semantic_leading_comment is None or before)
            ):
                self.semantic_leading_comment = block
        # `or before` means this decorator already has an empty line before
        elif not current_line.is_decorator or before:
            self.semantic_leading_comment = None

        self.previous_line = current_line
        self.previous_block = block
        return block

    def _maybe_empty_lines(self, current_line: Line) -> Tuple[int, int]:
        max_allowed = 1
        if len(current_line.depth) == 0:
            max_allowed = 1 if self.mode.is_pyi else 2
        if current_line.leaves:
            # Consume the first leaf's extra newlines.
            first_leaf = current_line.leaves[0]
            before = first_leaf.prefix.count("\n")
            before = min(before, max_allowed)
            first_leaf.prefix = ""
        else:
            before = 0
        depth = current_line.depth
        while self.previous_defs and len(self.previous_defs[-1].depth) >= len(depth):
            if self.mode.is_pyi:
                assert self.previous_line is not None
                if depth and not current_line.is_def and self.previous_line.is_def:
                    # Empty lines between attributes and methods should be preserved.
                    before = min(1, before)
                elif (
                    Preview.blank_line_after_nested_stub_class in self.mode
                    and self.previous_defs[-1].is_class
                    and not self.previous_defs[-1].is_stub_class
                ):
                    before = 1
                elif depth:
                    before = 0
                else:
                    before = 1
            else:
                if depth:
                    before = 1
                elif (
                    not depth
                    and self.previous_defs[-1].depth
                    and current_line.leaves[-1].type == token.COLON
                    and (
                        current_line.leaves[0].value
                        not in ("with", "try", "for", "while", "if", "match")
                    )
                ):
                    # We shouldn't add two newlines between an indented function and
                    # a dependent non-indented clause. This is to avoid issues with
                    # conditional function definitions that are technically top-level
                    # and therefore get two trailing newlines, but look weird and
                    # inconsistent when they're followed by elif, else, etc. This is
                    # worse because these functions only get *one* preceding newline
                    # already.
                    before = 1
                else:
                    before = 2
            self.previous_defs.pop()
        if current_line.is_decorator or current_line.is_def or current_line.is_class:
            return self._maybe_empty_lines_for_class_or_def(current_line, before)

        if (
            self.previous_line
            and self.previous_line.is_import
            and not current_line.is_import
            and not current_line.is_fmt_pass_converted(first_leaf_matches=is_import)
            and len(depth) == len(self.previous_line.depth)
        ):
            return (before or 1), 0

        if (
            self.previous_line
            and self.previous_line.is_class
            and current_line.is_triple_quoted_string
        ):
            return before, 1

        if self.previous_line and self.previous_line.opens_block:
            return 0, 0
        return before, 0

    def _maybe_empty_lines_for_class_or_def(
            self, current_line: Line, before: int
    ) -> Tuple[int, int]:
        if not current_line.is_decorator:
            self.previous_defs.append(current_line)
        if self.previous_line is None:
            # Don't insert empty lines before the first line in the file.
            return 0, 0

        if self.previous_line.is_decorator:
            if self.mode.is_pyi and current_line.is_stub_class:
                # Insert an empty line after a decorated stub class
                return 0, 1

            return 0, 0

        if len(self.previous_line.depth) < len(current_line.depth) and (
            self.previous_line.is_class or self.previous_line.is_def
        ):
            return 0, 0

        comment_to_add_newlines: Optional[LinesBlock] = None
        if (
            self.previous_line.is_comment
            and len(self.previous_line.depth) == len(current_line.depth)
            and before == 0
        ):
            slc = self.semantic_leading_comment
            if (
                slc is not None
                and slc.previous_block is not None
                and not slc.previous_block.original_line.is_class
                and not slc.previous_block.original_line.opens_block
                and slc.before <= 1
            ):
                comment_to_add_newlines = slc
            else:
                return 0, 0

        if self.mode.is_pyi:
            if current_line.is_class or self.previous_line.is_class:
                if len(self.previous_line.depth) < len(current_line.depth):
                    newlines = 0
                elif len(self.previous_line.depth) > len(current_line.depth):
                    newlines = 1
                elif current_line.is_stub_class and self.previous_line.is_stub_class:
                    # No blank line between classes with an empty body
                    newlines = 0
                else:
                    newlines = 1
            elif (
                current_line.is_def or current_line.is_decorator
            ) and not self.previous_line.is_def:
                if current_line.depth:
                    # In classes empty lines between attributes and methods should
                    # be preserved.
                    newlines = min(1, before)
                else:
                    # Blank line between a block of functions (maybe with preceding
                    # decorators) and a block of non-functions
                    newlines = 1
            elif len(self.previous_line.depth) > len(current_line.depth):
                newlines = 1
            else:
                newlines = 0
        else:
            newlines = 1 if current_line.depth else 2
        if comment_to_add_newlines is not None:
            previous_block = comment_to_add_newlines.previous_block
            if previous_block is not None:
                comment_to_add_newlines.before = (
                    max(comment_to_add_newlines.before, newlines) - previous_block.after
                )
                newlines = 0
        return newlines, 0


def enumerate_reversed(sequence: Sequence[T]) -> Iterator[Tuple[Index, T]]:
    """Like `reversed(enumerate(sequence))` if that were possible."""
    index = len(sequence) - 1
    for element in reversed(sequence):
        yield (index, element)
        index -= 1


def append_leaves(
        new_line: Line, old_line: Line, leaves: List[Leaf], preformatted: bool = False
) -> None:
    """
    Append leaves (taken from @old_line) to @new_line, making sure to fix the
    underlying Node structure where appropriate.

    All of the leaves in @leaves are duplicated. The duplicates are then
    appended to @new_line and used to replace their originals in the underlying
    Node structure. Any comments attached to the old leaves are reattached to
    the new leaves.

    Pre-conditions:
        set(@leaves) is a subset of set(@old_line.leaves).
    """
    for old_leaf in leaves:
        new_leaf = Leaf(old_leaf.type, old_leaf.value)
        replace_child(old_leaf, new_leaf)
        new_line.append(new_leaf, preformatted=preformatted)

        for comment_leaf in old_line.comments_after(old_leaf):
            new_line.append(comment_leaf, preformatted=True)


def is_line_short_enough(  # noqa: C901
        line: Line, *, mode: Mode, line_str: str = ""
) -> bool:
    """For non-multiline strings, return True if `line` is no longer than `line_length`.
    For multiline strings, looks at the context around `line` to determine
    if it should be inlined or split up.
    Uses the provided `line_str` rendering, if any, otherwise computes a new one.
    """
    if not line_str:
        line_str = line_to_string(line)

    if mode.use_tabs:
        # Override previous calculation of `line_str`, because we need
        # to treat the width of '\t' as more than 1 character
        line_str = line.render_as_str_for_width_calculation().strip("\n")

    width = str_width if mode.preview else len
    if mode.wrap_pragma_comments:  # Black's default
        effective_length = width(line_str)
    else:  # Cercis's default
        effective_length = width(line_str) - line.trailing_pragma_comment_length()

    if Preview.multiline_string_handling not in mode:
        return (
            effective_length <= mode.line_length
            and "\n" not in line_str  # multiline strings
            and not line.contains_standalone_comments()
        )

    if line.contains_standalone_comments():
        return False
    if "\n" not in line_str:
        # No multiline strings (MLS) present
        return effective_length <= mode.line_length

    first, *_, last = line_str.split("\n")
    if width(first) > mode.line_length or width(last) > mode.line_length:
        return False

    # Traverse the AST to examine the context of the multiline string (MLS),
    # tracking aspects such as depth and comma existence,
    # to determine whether to split the MLS or keep it together.
    # Depth (which is based on the existing bracket_depth concept)
    # is needed to determine nesting level of the MLS.
    # Includes special case for trailing commas.
    commas: List[int] = []  # tracks number of commas per depth level
    multiline_string: Optional[Leaf] = None
    # store the leaves that contain parts of the MLS
    multiline_string_contexts: List[LN] = []

    max_level_to_update: Union[int, float] = math.inf  # track the depth of the MLS
    for i, leaf in enumerate(line.leaves):
        if max_level_to_update == math.inf:
            had_comma: Optional[int] = None
            if leaf.bracket_depth + 1 > len(commas):
                commas.append(0)
            elif leaf.bracket_depth + 1 < len(commas):
                had_comma = commas.pop()
            if (
                had_comma is not None
                and multiline_string is not None
                and multiline_string.bracket_depth == leaf.bracket_depth + 1
            ):
                # Have left the level with the MLS, stop tracking commas
                max_level_to_update = leaf.bracket_depth
                if had_comma > 0:
                    # MLS was in parens with at least one comma - force split
                    return False

        if leaf.bracket_depth <= max_level_to_update and leaf.type == token.COMMA:
            # Ignore non-nested trailing comma
            # directly after MLS/MLS-containing expression
            ignore_ctxs: List[Optional[LN]] = [None]
            ignore_ctxs += multiline_string_contexts
            if not (leaf.prev_sibling in ignore_ctxs and i == len(line.leaves) - 1):
                commas[leaf.bracket_depth] += 1
        if max_level_to_update != math.inf:
            max_level_to_update = min(max_level_to_update, leaf.bracket_depth)

        if is_multiline_string(leaf):
            if len(multiline_string_contexts) > 0:
                # >1 multiline string cannot fit on a single line - force split
                return False
            multiline_string = leaf
            ctx: LN = leaf
            # fetch the leaf components of the MLS in the AST
            while str(ctx) in line_str:
                multiline_string_contexts.append(ctx)
                if ctx.parent is None:
                    break
                ctx = ctx.parent

    # May not have a triple-quoted multiline string at all,
    # in case of a regular string with embedded newlines and line continuations
    if len(multiline_string_contexts) == 0:
        return True

    return all(val == 0 for val in commas)


def can_be_split(line: Line) -> bool:
    """Return False if the line cannot be split *for sure*.

    This is not an exhaustive search but a cheap heuristic that we can use to
    avoid some unfortunate formattings (mostly around wrapping unsplittable code
    in unnecessary parentheses).
    """
    leaves = line.leaves
    if len(leaves) < 2:
        return False

    if leaves[0].type == token.STRING and leaves[1].type == token.DOT:
        call_count = 0
        dot_count = 0
        next = leaves[-1]
        for leaf in leaves[-2::-1]:
            if leaf.type in OPENING_BRACKETS:
                if next.type not in CLOSING_BRACKETS:
                    return False

                call_count += 1
            elif leaf.type == token.DOT:
                dot_count += 1
            elif leaf.type == token.NAME:
                if not (next.type == token.DOT or next.type in OPENING_BRACKETS):
                    return False

            elif leaf.type not in CLOSING_BRACKETS:
                return False

            if dot_count > 1 and call_count > 1:
                return False

    return True


def can_omit_invisible_parens(
        rhs: RHSResult,
        line_length: int,
) -> bool:
    """Does `rhs.body` have a shape safe to reformat without optional parens around it?

    Returns True for only a subset of potentially nice looking formattings but
    the point is to not return false positives that end up producing lines that
    are too long.
    """
    line = rhs.body
    bt = line.bracket_tracker
    if not bt.delimiters:
        # Without delimiters the optional parentheses are useless.
        return True

    max_priority = bt.max_delimiter_priority()
    delimiter_count = bt.delimiter_count_with_priority(max_priority)
    if delimiter_count > 1:
        # With more than one delimiter of a kind the optional parentheses read better.
        return False

    if delimiter_count == 1:
        if (
            Preview.wrap_multiple_context_managers_in_parens in line.mode
            and max_priority == COMMA_PRIORITY
            and rhs.head.is_with_or_async_with_stmt
        ):
            # For two context manager with statements, the optional parentheses read
            # better. In this case, `rhs.body` is the context managers part of
            # the with statement. `rhs.head` is the `with (` part on the previous
            # line.
            return False
        # Otherwise it may also read better, but we don't do it today and requires
        # careful considerations for all possible cases. See
        # https://github.com/psf/black/issues/2156.

    if max_priority == DOT_PRIORITY:
        # A single stranded method call doesn't require optional parentheses.
        return True

    assert len(line.leaves) >= 2, "Stranded delimiter"

    # With a single delimiter, omit if the expression starts or ends with
    # a bracket.
    first = line.leaves[0]
    second = line.leaves[1]
    if first.type in OPENING_BRACKETS and second.type not in CLOSING_BRACKETS:
        if _can_omit_opening_paren(line, first=first, line_length=line_length):
            return True

        # Note: we are not returning False here because a line might have *both*
        # a leading opening bracket and a trailing closing bracket.  If the
        # opening bracket doesn't match our rule, maybe the closing will.

    penultimate = line.leaves[-2]
    last = line.leaves[-1]

    if (
        last.type == token.RPAR
        or last.type == token.RBRACE
        or (
            # don't use indexing for omitting optional parentheses;
            # it looks weird
            last.type == token.RSQB
            and last.parent
            and last.parent.type != syms.trailer
        )
    ):
        if penultimate.type in OPENING_BRACKETS:
            # Empty brackets don't help.
            return False

        if is_multiline_string(first):
            # Additional wrapping of a multiline string in this situation is
            # unnecessary.
            return True

        if _can_omit_closing_paren(line, last=last, line_length=line_length):
            return True

    return False


def _can_omit_opening_paren(line: Line, *, first: Leaf, line_length: int) -> bool:
    """See `can_omit_invisible_parens`."""
    remainder = False
    length: int = line.calc_total_indent_width()
    _index = -1
    for _index, leaf, leaf_length in line.enumerate_with_length():
        if leaf.type in CLOSING_BRACKETS and leaf.opening_bracket is first:
            remainder = True
        if remainder:
            length += leaf_length
            if length > line_length:
                break

            if leaf.type in OPENING_BRACKETS:
                # There are brackets we can further split on.
                remainder = False

    else:
        # checked the entire string and line length wasn't exceeded
        if len(line.leaves) == _index + 1:
            return True

    return False


def _can_omit_closing_paren(line: Line, *, last: Leaf, line_length: int) -> bool:
    """See `can_omit_invisible_parens`."""
    length: int = line.calc_total_indent_width()
    seen_other_brackets = False
    for _index, leaf, leaf_length in line.enumerate_with_length():
        length += leaf_length
        if leaf is last.opening_bracket:
            if seen_other_brackets or length <= line_length:
                return True

        elif leaf.type in OPENING_BRACKETS:
            # There are brackets we can further split on.
            seen_other_brackets = True

    return False


def line_to_string(line: Line) -> str:
    """Returns the string representation of @line.

    WARNING: This is known to be computationally expensive.
    """
    return str(line).strip("\n")