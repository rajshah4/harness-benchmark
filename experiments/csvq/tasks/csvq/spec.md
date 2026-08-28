# csvq — A CSV Query Tool

## Overview

`csvq` is a command-line tool for querying, filtering, sorting, and analyzing CSV data. It reads CSV from files or standard input and writes results to standard output.

## Usage

```
csvq <command> [options] [file]
```

If no file is specified, or the file is `-`, input is read from standard input.

## Commands

### select

Select specific columns by name.

```
csvq select <col1,col2,...> [file]
```

Outputs only the named columns, preserving the order requested. The header row is always included in output.

### filter

Filter rows where a column matches a condition.

```
csvq filter <column> <operator> <value> [file]
```

Outputs the full row (all columns) for matching rows, including the header.

### sort

Sort rows by a column.

```
csvq sort <column> [--reverse] [file]
```

Numeric columns are sorted numerically; text columns alphabetically. The header row always stays at the top.

### stats

Compute statistics for a numeric column.

```
csvq stats <column> [file]
```

Outputs a single row with summary statistics.

### join

Join two CSV files on a matching column.

```
csvq join <left_col> <right_col> <left_file> <right_file>
```

Inner join: only rows with matching keys appear in output.

## CSV Handling

- The first row is treated as the header.
- Fields containing commas, double quotes, or newlines must be quoted with double quotes.
- Embedded double quotes are escaped by doubling (`""`).
- Empty fields are valid.
- Column name matching is case-insensitive.

## What You Need to Build

Implement a binary called `csvq` that behaves identically to the reference implementation (the oracle). The oracle is available as a black-box binary at `./oracle-bin`. You may run it as many times as you want to discover its behavior, but you cannot read its source code.

## Deliverable

Your implementation should be a single executable binary named `csvq` located at `/home/openhands/candidate/csvq` (or the path you set in your final response). It must accept the same command-line interface and produce the same output as the oracle for the same inputs.

## Notes

- The spec above is intentionally incomplete. The oracle has behaviors not fully described here. You are expected to discover them by running the oracle.
- Edge cases in CSV parsing (quoting, embedded newlines, empty fields) are important.
- Error handling (missing files, unknown columns) should match the oracle's behavior.
- Output format (CSV quoting rules) must match exactly.
