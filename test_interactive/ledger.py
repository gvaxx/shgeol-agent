"""Simple ledger: tracks income and expenses."""

import utils


class Ledger:
    def __init__(self):
        self.entries = []

    def add(self, description, amount_str, kind):
        """Add an entry. kind must be 'income' or 'expense'."""
        if kind not in ("income", "expense"):
            raise ValueError(f"Invalid kind: {kind!r}")
        amount = utils.parse_amount(amount_str)
        self.entries.append({"desc": description, "amount": amount, "kind": kind})

    def balance(self):
        total = 0
        for e in self.entries:
            if e["kind"] == "income":
                total += e["amount"]   # bug: =+ instead of +=
            else:
                total -= e["amount"]
        return total

    def summary(self):
        inc = sum(e["amount"] for e in self.entries if e["kind"] == "income")
        exp = sum(e["amount"] for e in self.entries if e["kind"] == "expense")
        return {
            "income":  utils.format_amount(inc),
            "expense": utils.format_amount(exp),
            "balance": utils.format_amount(self.balance()),
        }

    def top_expenses(self, n=3):
        expenses = [e for e in self.entries if e["kind"] == "expense"]
        expenses.sort(key=lambda e: e["amount"], reverse=True)   # bug: ascending, should be descending
        return expenses[:n]
