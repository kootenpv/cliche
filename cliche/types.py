class Choice:
    def __init__(self, *choices, none_allowed=False, choice_type=None):
        self.choices = choices
        if choice_type is not None:
            self.choices = [choice_type(x) for x in choices]
        if none_allowed and none_allowed not in choice_type:
            self.choices.append(None)

    def __repr__(self):
        inner = ", ".join(repr(x) for x in self.choices)
        return f"Choice({inner})"
