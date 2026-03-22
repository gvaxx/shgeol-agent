"""Utility functions."""


def parse_amount(s):
    """Parse a money string like '$1,234.56' into a float."""
    s = s.strip().lstrip("$").replace(",", "")
    return float(s)


def format_amount(amount, currency="USD"):
    """Format a float as a money string."""
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    symbol = symbols.get(currency, "$")
    return f"{symbol}{amount:,.2f}"


def clamp(val, lo, hi):
    """Clamp val between lo and hi."""
    if val < lo:
        return lo
    if val > hi:
        return hi
    return val
