"""Generate text reports from a Ledger."""

import utils
from ledger import Ledger


def print_summary(ledger):
    s = ledger.summary()
    print("=== Summary ===")
    print(f"  Income:  {s['income']}")
    print(f"  Expense: {s['expense']}")
    print(f"  Balance: {s['balance']}")


def print_top_expenses(ledger, n=3):
    print(f"\n=== Top {n} Expenses ===")
    for e in ledger.top_expenses(n):
        print(f"  {e['desc']:30s} {utils.format_amount(e['amount'])}")


def demo():
    g = Ledger()
    g.add("Salary",        "$3,500.00", "income")
    g.add("Freelance",     "$800.00",   "income")
    g.add("Rent",          "$1,200.00", "expense")
    g.add("Groceries",     "$320.50",   "expense")
    g.add("Electricity",   "$95.00",    "expense")
    g.add("Internet",      "$45.00",    "expense")
    g.add("Gym",           "$30.00",    "expense")

    print_summary(g)
    print_top_expenses(g)


if __name__ == "__main__":
    demo()
