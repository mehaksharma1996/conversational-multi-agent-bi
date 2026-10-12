"""Load uploaded CSV and Excel files into pandas dataframes."""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from charset_normalizer import from_bytes

SUPPORTED_TABULAR_EXTENSIONS = {".csv", ".xls", ".xlsx"}


class TabularLoadError(ValueError):
    """Raised when an uploaded tabular file cannot be loaded."""


@dataclass(frozen=True)
class LoadedTable:
    dataframe: pd.DataFrame
    filename: str
    extension: str
    row_count: int
    column_count: int
    column_name_mapping: dict[str, str]
    sheet_name: str | None = None
    column_warnings: dict[str, list[str]] = field(default_factory=dict)


def load_tabular_file(
    file: BinaryIO,
    filename: str,
    *,
    max_rows: int | None = None,
    sheet_name: str | int = 0,
) -> LoadedTable:
    """Load a CSV or Excel upload and return dataframe metadata."""
    extension = Path(filename).suffix.lower()

    if extension not in SUPPORTED_TABULAR_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_TABULAR_EXTENSIONS))
        raise TabularLoadError(
            f"Unsupported file type '{extension or 'unknown'}'. "
            f"Supported tabular types are: {supported}."
        )

    try:
        dataframe = _read_dataframe(
            file=file,
            extension=extension,
            max_rows=max_rows,
            sheet_name=sheet_name,
        )
    except UnicodeDecodeError as exc:
        raise TabularLoadError(
            "Could not decode the CSV file. Please save it as UTF-8 and try again."
        ) from exc
    except Exception as exc:
        raise TabularLoadError(f"Could not load '{filename}': {exc}") from exc

    if dataframe.empty and len(dataframe.columns) == 0:
        raise TabularLoadError("The uploaded file did not contain any columns.")

    cleaned, column_name_mapping, column_warnings = clean_dataframe(dataframe)
    resolved_sheet = str(sheet_name) if extension in {".xls", ".xlsx"} else None
    return LoadedTable(
        dataframe=cleaned,
        filename=filename,
        extension=extension,
        row_count=len(cleaned),
        column_count=len(cleaned.columns),
        column_name_mapping=column_name_mapping,
        sheet_name=resolved_sheet,
        column_warnings=column_warnings,
    )


def list_excel_sheets(file: BinaryIO) -> list[str]:
    """Return workbook sheet names without loading their table contents."""
    file.seek(0)
    try:
        with pd.ExcelFile(file) as workbook:
            return list(workbook.sheet_names)
    except Exception as exc:
        raise TabularLoadError(f"Could not read Excel workbook sheets: {exc}") from exc


def clean_dataframe(
    dataframe: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, list[str]]]:
    """Normalize column names and strongly typed business values.

    Currency, percentage, and date-format normalization is lossy by nature
    (the original string formatting is not retained), so each column that
    was transformed, or where the transformation is ambiguous (mixed
    currencies, an unresolvable day/month order), gets a warning explaining
    what was assumed.
    """
    cleaned = dataframe.copy()
    cleaned_columns, mapping = _clean_column_names(cleaned.columns)
    cleaned.columns = cleaned_columns

    column_warnings: dict[str, list[str]] = {}
    for column in cleaned.columns:
        series = cleaned[column]
        numeric, warnings = _parse_formatted_numeric(series)
        if warnings:
            column_warnings[column] = warnings
        if numeric is not None:
            cleaned[column] = numeric
            continue
        if warnings:
            # Numeric-looking column that could not be converted safely: keep it as text
            # rather than letting date inference reinterpret it.
            continue
        date_result = _parse_date_series(series, column)
        if date_result is not None:
            dates, warnings = date_result
            cleaned[column] = dates
            if warnings:
                column_warnings[column] = warnings
    return cleaned, mapping, column_warnings


def _read_dataframe(
    file: BinaryIO,
    extension: str,
    max_rows: int | None,
    sheet_name: str | int,
) -> pd.DataFrame:
    file.seek(0)

    if extension == ".csv":
        payload = file.read()
        decoded = _decode_csv(payload)
        delimiter = _detect_delimiter(decoded)

        def read_raw_csv(positions: list[int]) -> pd.DataFrame:
            return pd.read_csv(
                StringIO(decoded), sep=delimiter, nrows=max_rows, dtype=str, usecols=positions
            )

        dataframe = pd.read_csv(StringIO(decoded), sep=delimiter, nrows=max_rows)
        return _restore_ambiguous_text(dataframe, read_raw_csv)

    def read_raw_excel(positions: list[int]) -> pd.DataFrame:
        file.seek(0)
        return pd.read_excel(
            file, sheet_name=sheet_name, nrows=max_rows, dtype=object, usecols=positions
        )

    dataframe = pd.read_excel(file, sheet_name=sheet_name, nrows=max_rows)
    return _restore_ambiguous_text(dataframe, read_raw_excel)


def _restore_ambiguous_text(
    dataframe: pd.DataFrame, read_raw: Callable[[list[int]], pd.DataFrame]
) -> pd.DataFrame:
    """Undo pandas' early float conversion of text whose number format is undecidable.

    pandas turns a text column such as ``1.000, 2.000, 3.500`` into the floats 1.0, 2.0 and
    3.5 while reading, before the loader can ask whether the dots are thousands separators.
    Only float columns can have lost information this way, so just those are re-read without
    type inference; a column goes back to its raw text only when every non-missing cell was
    text and all of it is ambiguous or plain (so no cell proves the dot is a decimal point).
    Native numeric cells (Excel) are never text, so they are left alone.
    """
    float_positions = [
        position
        for position, dtype in enumerate(dataframe.dtypes)
        if pd.api.types.is_float_dtype(dtype)
    ]
    if not float_positions:
        return dataframe
    raw = read_raw(float_positions)
    restored = dataframe.copy()
    for raw_position, position in enumerate(float_positions):
        raw_column = raw.iloc[:, raw_position]
        if _is_ambiguous_only_text(raw_column):
            restored.isetitem(position, raw_column)
    return restored


def _is_ambiguous_only_text(column: pd.Series) -> bool:
    values = column.dropna()
    if values.empty or not all(isinstance(value, str) for value in values):
        return False
    tokens = [_tokenize_number(value.strip()) for value in values]
    if any(token is None for token in tokens):
        return False
    styles = {token.style for token in tokens if token is not None}
    return _AMBIGUOUS in styles and styles <= {_AMBIGUOUS, _PLAIN}


def _decode_csv(payload: bytes) -> str:
    if not payload:
        return ""
    match = from_bytes(payload).best()
    if match is None:
        raise UnicodeDecodeError("unknown", payload, 0, len(payload), "encoding not detected")
    return str(match)


def _detect_delimiter(text: str) -> str:
    sample = text[:16_384]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _clean_column_names(columns: pd.Index) -> tuple[list[str], dict[str, str]]:
    cleaned: list[str] = []
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    for raw_column in columns:
        original = str(raw_column).strip()
        base = re.sub(r"[^a-zA-Z0-9_]+", "_", original).strip("_").lower() or "column"
        if base[0].isdigit():
            base = f"column_{base}"
        counts[base] = counts.get(base, 0) + 1
        safe_name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        cleaned.append(safe_name)
        mapping[original] = safe_name
    return cleaned, mapping


_CURRENCY_SYMBOLS = "$€£¥"
_CURRENCY_NAMES = {
    "$": "USD-style ($)",
    "€": "EUR-style (€)",
    "£": "GBP-style (£)",
    "¥": "JPY-style (¥)",
}


_NUMBER_PATTERN = re.compile(
    r"(?P<open>\()?\s*(?P<sign>[-+])?\s*(?:[$€£¥]\s*)?(?P<body>\d[\d.,]*)\s*(?P<percent>%)?\s*"
    r"(?P<close>\))?"
)
_NUMERIC_LOOKING_PATTERN = re.compile(r"[\s()+\-$€£¥]*\d[\d.,\s]*%?[\s)]*")
_NUMERIC_MARKER_PATTERN = re.compile(
    r"[$€£¥,%()]|\d\.\d{3}\.\d|(?<![\d.])[1-9]\d{0,2}\.\d{3}(?![\d.,])"
)
_US_GROUPED = re.compile(r"[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?")
_EU_GROUPED = re.compile(r"[1-9]\d{0,2}(?:\.\d{3})+(?:,\d+)?")

# Share of values that must be readable numbers before a column is converted, and the share
# of numeric-looking values above which an unconverted column is reported rather than ignored.
_NUMERIC_COLUMN_MIN_RATIO = 0.9
_NUMERIC_LOOKING_WARN_RATIO = 0.5
_MAX_REPORTED_ROWS = 5

# Separator styles a value (or a whole column) can be written in.
_US = "us"  # 1,234.56
_EU = "eu"  # 1.234,56
_PLAIN = "plain"  # 1234: readable in every style
_AMBIGUOUS = "ambiguous"  # 1,234 or 1.234: thousands group or decimals, depending on style
_SEPARATORS = {_US: (",", "."), _EU: (".", ",")}  # (thousands, decimal)


@dataclass(frozen=True)
class _NumberToken:
    body: str
    style: str
    negative: bool
    percent: bool


def _classify_number_body(body: str) -> str | None:
    """Return the separator style a digit/separator string proves, or None if unreadable.

    Single-separator values with exactly three trailing digits, such as ``1,000``, are
    ambiguous; every other readable form proves one style.
    """
    if body.isdigit():
        return _PLAIN
    if _US_GROUPED.fullmatch(body):
        return _US if body.count(",") > 1 or "." in body else _AMBIGUOUS
    if _EU_GROUPED.fullmatch(body):
        return _EU if body.count(".") > 1 or "," in body else _AMBIGUOUS
    if re.fullmatch(r"\d+\.\d+", body):
        return _US
    if re.fullmatch(r"\d+,\d+", body):
        return _EU
    return None


def _tokenize_number(text: str) -> _NumberToken | None:
    match = _NUMBER_PATTERN.fullmatch(text)
    if match is None:
        return None
    parenthesised = match["open"] is not None
    if parenthesised != (match["close"] is not None):
        return None
    if parenthesised and match["sign"] is not None:
        return None  # "(-5)" is contradictory; refuse rather than guess the sign
    style = _classify_number_body(match["body"])
    if style is None:
        return None
    return _NumberToken(
        body=match["body"],
        style=style,
        negative=parenthesised or match["sign"] == "-",
        percent=match["percent"] is not None,
    )


def _number_value(token: _NumberToken, style: str) -> float:
    thousands, decimal = _SEPARATORS[style]
    number = float(token.body.replace(thousands, "").replace(decimal, "."))
    if token.negative:
        number = -number
    return number / 100 if token.percent else number


def _parse_formatted_numeric(series: pd.Series) -> tuple[pd.Series | None, list[str]]:
    """Convert a text column of formatted numbers, or explain why it was left alone.

    The decimal/thousands convention is decided per column from values that can only be
    read one way (``10,5``, ``1.234,56``, ``1,234.56``). Columns that contain both
    conventions are never converted. Columns whose values are all ambiguous (``1,000``) or
    plain (``1000``) prove no convention, so they are left as text with a warning.
    """
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return None, []
    non_null = series.dropna().astype(str).str.strip()
    if non_null.empty:
        return None, []
    if not non_null.str.contains(_NUMERIC_MARKER_PATTERN).any():
        return None, _stray_text_in_plain_numbers_warning(series, non_null)

    tokens = [_tokenize_number(value) for value in non_null]
    readable = [token for token in tokens if token is not None]
    readable_ratio = len(readable) / len(tokens)

    if readable_ratio < _NUMERIC_COLUMN_MIN_RATIO:
        looking_ratio = float(non_null.map(_NUMERIC_LOOKING_PATTERN.fullmatch).notna().mean())
        if looking_ratio < _NUMERIC_LOOKING_WARN_RATIO:
            return None, []
        return None, [
            f"Only {len(readable)} of {len(tokens)} values in this column could be read as "
            "numbers, so the column was left as text. Check the number format (for example "
            "1,234.56 vs 1.234,56) and re-upload."
        ]

    styles = {token.style for token in readable}
    if _US in styles and _EU in styles:
        return None, [
            "Mixed number formats detected (both 1,234.56-style and 1.234,56-style values); "
            "the column was left as text because converting it would change values. "
            "Use one number format per column and re-upload."
        ]

    if _AMBIGUOUS in styles and _US not in styles and _EU not in styles:
        return None, [
            "Number format could not be determined from the data (values such as 1,000 or "
            "1.000 can be thousands or decimals), so the column was left as text rather than "
            "guessing. Use an unambiguous format (e.g., 1000, 1,000.00 or 1.000,00) and "
            "re-upload."
        ]

    style = _EU if _EU in styles else _US
    warnings: list[str] = []
    if style == _EU:
        warnings.append(
            "Decimal-comma number format detected (e.g., 1.234,56); values were converted "
            "to plain numbers (1234.56)."
        )

    unreadable_rows: list[int] = []
    values: list[float] = []
    for position, value in enumerate(series, start=1):
        if pd.isna(value):
            values.append(float("nan"))
            continue
        token = _tokenize_number(str(value).strip())
        if token is None:
            unreadable_rows.append(position)
            values.append(float("nan"))
        else:
            values.append(_number_value(token, style))
    if unreadable_rows:
        warnings.append(
            f"{len(unreadable_rows)} of {len(tokens)} values could not be read as numbers "
            f"({_describe_rows(unreadable_rows)}) and were set to missing."
        )
    warnings.extend(_formatted_numeric_warnings(non_null))
    return pd.Series(values, index=series.index, dtype="float64"), warnings


def _describe_rows(rows: list[int]) -> str:
    shown = ", ".join(str(row) for row in rows[:_MAX_REPORTED_ROWS])
    return f"data rows {shown}{'' if len(rows) <= _MAX_REPORTED_ROWS else ', ...'}"


def _stray_text_in_plain_numbers_warning(series: pd.Series, non_null: pd.Series) -> list[str]:
    """Warn when a column of unformatted numbers is held back as text by a few invalid cells.

    The column is not converted (integer-looking text such as ``00123`` is often an
    identifier), but it must not silently become categorical.
    """
    is_plain_number = series.astype(str).str.strip().str.fullmatch(r"[-+]?\d+(?:\.\d+)?")
    plain_ratio = float(non_null.str.fullmatch(r"[-+]?\d+(?:\.\d+)?").mean())
    if not _NUMERIC_COLUMN_MIN_RATIO <= plain_ratio < 1.0:
        return []
    stray_rows = [
        position
        for position, (value, plain) in enumerate(zip(series, is_plain_number, strict=True), 1)
        if not pd.isna(value) and not plain
    ]
    return [
        f"{len(stray_rows)} of {len(non_null)} values in this mostly numeric column are not "
        f"numbers ({_describe_rows(stray_rows)}), so the column was kept as text and will not "
        "be treated as numeric. Fix or remove those values and re-upload."
    ]


def _formatted_numeric_warnings(non_null: pd.Series) -> list[str]:
    warnings: list[str] = []
    currencies_used = {
        symbol
        for symbol in _CURRENCY_SYMBOLS
        if non_null.str.contains(re.escape(symbol), regex=True).any()
    }
    if len(currencies_used) > 1:
        names = ", ".join(sorted(_CURRENCY_NAMES[symbol] for symbol in currencies_used))
        warnings.append(
            f"Mixed currency symbols detected ({names}); all values were converted to "
            "plain numbers with no currency conversion applied. Verify this is correct."
        )
    elif len(currencies_used) == 1:
        symbol = next(iter(currencies_used))
        warnings.append(
            f"Currency symbol removed ({_CURRENCY_NAMES[symbol]}); values are now plain "
            "numbers with the original currency unit no longer recorded."
        )
    if non_null.str.endswith("%").any():
        warnings.append("Percentage values were converted to a fraction (e.g., 12% became 0.12).")
    return warnings


_SLASH_OR_DASH_DATE_PATTERN = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-]\d{2,4}")


def _parse_date_series(series: pd.Series, column_name: str) -> tuple[pd.Series, list[str]] | None:
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return None
    non_null = series.dropna()
    if non_null.empty:
        return None
    sample = non_null.head(1_000)
    parsed_sample = pd.to_datetime(sample, errors="coerce", format="mixed")
    name_hint = any(token in column_name for token in ("date", "time", "timestamp"))
    if float(parsed_sample.notna().mean()) < (0.8 if name_hint else 0.95):
        return None

    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if not parsed.notna().any():
        return None
    includes_time = bool(
        ((parsed.dt.hour != 0) | (parsed.dt.minute != 0) | (parsed.dt.second != 0)).any()
    )
    date_format = "%Y-%m-%d %H:%M:%S" if includes_time else "%Y-%m-%d"
    formatted = parsed.dt.strftime(date_format)
    warnings = _date_format_warnings(non_null)
    return formatted.where(parsed.notna(), None), warnings


def _date_format_warnings(non_null: pd.Series) -> list[str]:
    """Warn when day/month order cannot be determined from the data itself.

    If every numeric-slash/dash date in the column has both leading
    components <= 12, DD/MM and MM/DD are both plausible and pandas' format
    inference is guessing; if any value has a component > 12, the order is
    already unambiguous from the data and no warning is needed.
    """
    pairs = []
    for value in non_null.astype(str).str.strip():
        match = _SLASH_OR_DASH_DATE_PATTERN.match(value)
        if match:
            pairs.append((int(match.group(1)), int(match.group(2))))
    if not pairs:
        return []
    if all(first <= 12 and second <= 12 for first, second in pairs):
        return [
            "Date order (day/month vs month/day) could not be determined from the data "
            "(all values have both components 12 or below); dates were parsed assuming "
            "the locale's default order and may be misread if that assumption is wrong."
        ]
    return []
