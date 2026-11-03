"""The routing recommendation without a model: the title and description, then the category."""

from app.modules.tickets.triage import DEFAULT_CATEGORIES, CategoryOption, recommend

OTHER = next(c for c in DEFAULT_CATEGORIES if c.id == "default:other")
BRANCH = next(c for c in DEFAULT_CATEGORIES if c.id == "default:branch")
TECHNICAL = next(c for c in DEFAULT_CATEGORIES if c.id == "default:technical")


def advise(category, subject, description=""):
    return recommend(None, category=category, subject=subject, description=description, skills=[],
                     categories=DEFAULT_CATEGORIES)


def test_the_words_of_the_ticket_recommend_when_the_category_does_not():
    advice = advise(OTHER, "Not able to use the platform, button not clickable")
    assert (advice.team, advice.source) == ("technical", "text")
    assert "“platform”" in advice.reason and advice.needs_choice(0.6)  # a suggestion, never certain

    advice = advise(OTHER, "Cash counter short-staffed", "Customers waited an hour at the branch today.")
    assert (advice.team, advice.source) == ("branch", "text")

    assert advise(OTHER, "Can't log in", "").team == "technical"  # words run together: "log in" is "login"


def test_the_ticket_may_outweigh_the_category_and_the_category_breaks_a_tie():
    advice = advise(BRANCH, "Upload page shows an error", "The file upload fails with a timeout.")
    assert advice.team == "technical" and "rather than" in advice.reason
    # One word each way: the category the creator chose decides.
    assert advise(TECHNICAL, "Branch screen").team == "technical"
    assert advise(BRANCH, "Branch screen").team == "branch"


def test_with_nothing_to_go_on_the_category_or_the_creator_decides():
    assert advise(BRANCH, "Hello", "Please help").source == "category"
    advice = advise(OTHER, "Hello", "Please help")
    assert (advice.team, advice.source) == (None, "none")


def test_an_organizations_own_categories_add_their_words():
    atm = CategoryOption("x", "ATM machines", "technical", "Card readers and dispensers")
    advice = recommend(None, category=OTHER, subject="The dispenser jammed", description="", skills=[],
                       categories=[*DEFAULT_CATEGORIES, atm])
    assert advice.team == "technical"
