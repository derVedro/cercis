class az:
    def __init__(self, buki):
        self.buki = buki

    def __str__(self):
        """
        some simple method
        """
        vedi = 42

        return f"{type(self).__name__}:{str(self.buki)}"

class glagoli:

    def __init__(self):
        self.dobro = 23

    def __str__(self):
        return f"{type(self).__name__}:{str(self.dobro)}"


class yest:

    def __init__(self, zhivete):
        self.zhivete = zhivete


    def __str__(self):
        return f"{type(self).__name__}:{str(self.zhivete)}"

    try:
        zelo = "zemlja" + 13
    except TypeError:
        pass