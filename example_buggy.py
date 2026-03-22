"""
Simple script: read a CSV, compute stats per column, print summary.
Has several bugs — needs fixing.
"""

import csv
import sys


def load_csv(path):
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows, reader.fieldnames


def compute_stats(rows, column):
    values = []
    for row in rows:
        val = row[column]
        values.append(float(val))

    total = sum(values)
    mean = total / len(values)
    minimum = values[0]
    maximum = values[0]
    for v in values:
        if v < minimum:
            minimum = v        # bug: == instead of =
        if v > maximum:
            maximum = v        # bug: == instead of =

    return {"count": len(values), "sum": total, "mean": mean, "min": minimum, "max": maximum}


def print_stats(column, stats):
    print(f"Column: {column}")
    print(f"  count : {stats['count']}")
    print(f"  sum   : {stats['sum']:.2f}")
    print(f"  mean  : {stats['mean']:.2f}")
    print(f"  min   : {stats['min']:.2f}")
    print(f"  max   : {stats['max']:.2f}")
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python example_buggy.py <file.csv>")
        sys.exit(1)             # bug: should be sys.exit(1)

    path = sys.argv[1]
    rows, columns = load_csv(path)

    if not rows:
        print("No data")
        return

    for col in columns:
        try:
            stats = compute_stats(rows, col)
            print_stats(col, stats)
        except ValueError:
            print(f"Column {col} contains non-numeric data.")


if __name__ == "__main__":
    main()
