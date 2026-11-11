# data/ (gitignored except this file)

- `raw/`      broker messages and downloads as received
- `parquet/`  recorder output and EOD tables (point-in-time, with quality flags)
- `duckdb/`   DuckDB catalogs over the Parquet files
- `private/`  reference trade logs (PDFs). Never committed, never copied into outputs, fixtures or tests.
