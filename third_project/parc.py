import calendar
import re
import time
import json
from datetime import date, timedelta
from pathlib import Path
import sys
import pandas as pd
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
MULTITHREAD_ENABLED = True
MAX_WORKERS = 10
def get_base_dir() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent
# =========================
# НАСТРОЙКИ
# =========================
BASE_DIR = get_base_dir()
STATIONS_CSV_PATH = BASE_DIR / "stations_with_data_in_last_5_days.csv"
OUTPUT_DIR = Path("output_pogodaiklimat")
def process_station_worker(
    station_id: str,
    station_name: str,
    base_output_dir: Path,
    log=None,
    stop_event=None,
) -> bool:
    session = make_session()
    return process_station(
        session=session,
        station_id=station_id,
        station_name=station_name,
        base_output_dir=base_output_dir,
        log=log,
        stop_event=stop_event,
    )
# ТЕСТ: март-апрель 2021
START_DATE = date(2021, 3, 1)
END_DATE = date.today() - timedelta(days=1)

# ТЕСТ: только 2 станции
TARGET_STATION_IDS = None

REQUEST_TIMEOUT = 60
SLEEP_BETWEEN_REQUESTS = 1.5
MAX_RETRIES = 3

BASE_URL = "https://www.pogodaiklimat.ru/weather.php"


# =========================
# ВСПОМОГАТЕЛЬНОЕ
# =========================
def ensure_output_dir(base_output_dir: Path) -> None:
    base_output_dir.mkdir(parents=True, exist_ok=True)

def normalize_text(text: str) -> str:
    if text is None:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def month_iter(start_date: date, end_date: date):
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        yield y, m
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def get_requested_months(start_date: date, end_date: date) -> list[str]:
    return [month_key(y, m) for y, m in month_iter(start_date, end_date)]


def load_station_meta(station_output_dir: Path, station_id: str) -> dict:
    meta_path = station_output_dir / f"{station_id}_META.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_station_meta(station_output_dir: Path, station_id: str, meta: dict) -> None:
    meta_path = station_output_dir / f"{station_id}_META.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
def normalize_for_merge(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    if "station_id" in df.columns:
        df["station_id"] = df["station_id"].astype(str).str.strip()

    if "date_utc" in df.columns:
        df["date_utc"] = pd.to_datetime(
            df["date_utc"],
            errors="coerce",
            format="%Y-%m-%d"
        ).dt.strftime("%Y-%m-%d")

    if "time_utc" in df.columns:
        df["time_utc"] = (
            pd.to_numeric(df["time_utc"], errors="coerce")
            .astype("Int64")
        )

    return df

def build_missing_ranges(expected_dates: set[date], actual_dates: set[date]) -> list[dict]:
    missing_dates = sorted(expected_dates - actual_dates)
    if not missing_dates:
        return []

    ranges = []
    start = missing_dates[0]
    prev = missing_dates[0]

    for current in missing_dates[1:]:
        if (current - prev).days == 1:
            prev = current
            continue

        ranges.append(
            {
                "from": start.isoformat(),
                "to": prev.isoformat(),
                "days": (prev - start).days + 1,
            }
        )
        start = current
        prev = current

    ranges.append(
        {
            "from": start.isoformat(),
            "to": prev.isoformat(),
            "days": (prev - start).days + 1,
        }
    )
    return ranges


def rebuild_station_meta_from_df(
    station_id: str,
    station_name: str,
    final_df: pd.DataFrame,
    expected_start_date: date,
    expected_end_date: date,
) -> dict:
    result = {
        "station_id": station_id,
        "station_name": station_name,
        "status": "no_csv",
        "row_count": 0,
        "first_datetime": "",
        "last_datetime": "",
        "first_date": "",
        "last_date": "",
        "date_count": 0,
        "duplicate_timestamp_count": 0,
        "missing_days_count": 0,
        "missing_ranges_count": 0,
        "missing_ranges_preview": "",
        "missing_ranges": [],
        "months_present": [],
        "months_missing": [],
        "month_stats": {},
        "error": "",
    }

    if final_df is None or final_df.empty:
        return result

    work_df = final_df.copy()
    result["row_count"] = int(len(work_df))

    if "station_name_pdf" in work_df.columns:
        names = work_df["station_name_pdf"].dropna().astype(str).str.strip()
        if not names.empty:
            result["station_name"] = names.iloc[0]

    if "date_utc" not in work_df.columns or "time_utc" not in work_df.columns:
        result["status"] = "required_columns_missing"
        result["error"] = "Нет колонок date_utc/time_utc"
        return result

    parsed_date = pd.to_datetime(work_df["date_utc"], errors="coerce", format="%Y-%m-%d")
    time_part = pd.to_numeric(work_df["time_utc"], errors="coerce").fillna(0)
    parsed_dt = parsed_date + pd.to_timedelta(time_part, unit="h")

    valid_mask = parsed_date.notna()
    valid_dates = parsed_date[valid_mask].dt.date
    valid_dt = parsed_dt[valid_mask]

    if valid_dates.empty:
        result["status"] = "no_valid_datetimes"
        result["error"] = "Не удалось распознать date_utc/time_utc"
        return result

    result["duplicate_timestamp_count"] = int(valid_dt.duplicated().sum())

    first_dt = valid_dt.min()
    last_dt = valid_dt.max()

    result["first_datetime"] = first_dt.isoformat(sep=" ")
    result["last_datetime"] = last_dt.isoformat(sep=" ")
    result["first_date"] = first_dt.date().isoformat()
    result["last_date"] = last_dt.date().isoformat()

    actual_dates = set(valid_dates.tolist())
    result["date_count"] = len(actual_dates)

    expected_dates = {
        d.date()
        for d in pd.date_range(start=expected_start_date, end=expected_end_date, freq="D")
    }
    missing_ranges = build_missing_ranges(expected_dates, actual_dates)

    result["missing_days_count"] = sum(r["days"] for r in missing_ranges)
    result["missing_ranges_count"] = len(missing_ranges)
    result["missing_ranges"] = missing_ranges

    preview_parts = []
    for r in missing_ranges[:5]:
        preview_parts.append(r["from"] if r["from"] == r["to"] else f"{r['from']}..{r['to']}")
    result["missing_ranges_preview"] = "; ".join(preview_parts)

    month_df = pd.DataFrame(
        {
            "date": valid_dates.astype(str),
            "timestamp": valid_dt.astype("datetime64[ns]"),
        }
    )
    month_df["month_key"] = month_df["timestamp"].dt.strftime("%Y-%m")

    months_present = sorted(month_df["month_key"].dropna().unique().tolist())
    month_stats = {}

    for mkey, grp in month_df.groupby("month_key"):
        month_stats[mkey] = {
            "row_count": int(len(grp)),
            "unique_dates": int(grp["date"].nunique()),
            "duplicate_timestamps": int(grp["timestamp"].duplicated().sum()),
            "first_timestamp": grp["timestamp"].min().isoformat(sep=" "),
            "last_timestamp": grp["timestamp"].max().isoformat(sep=" "),
        }

    expected_months = get_requested_months(expected_start_date, expected_end_date)
    months_missing = sorted(set(expected_months) - set(months_present))

    result["months_present"] = months_present
    result["months_missing"] = months_missing
    result["month_stats"] = month_stats
    result["status"] = "complete" if result["missing_days_count"] == 0 else "has_gaps"

    return result


def load_stations_from_csv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"Файл со станциями не найден: {csv_path}")

    df = pd.read_csv(csv_path)

    required_columns = {"station_id", "station_name"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(
            f"В CSV нет нужных колонок: {sorted(missing)}. "
            f"Ожидались: {sorted(required_columns)}"
        )

    df = df.rename(columns={"station_id": "wmo_id"})
    df["wmo_id"] = df["wmo_id"].astype(str).str.strip()
    df["station_name"] = df["station_name"].astype(str).str.strip()

    df = df.dropna(subset=["wmo_id"])
    df = df[df["wmo_id"] != ""]

    return df[["wmo_id", "station_name"]].drop_duplicates()


def build_url(station_id: str, year: int, month: int) -> str:
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])

    real_start = max(START_DATE, month_start)
    real_end = min(END_DATE, month_end)

    bday = real_start.day
    fday = real_end.day

    return (
        f"{BASE_URL}?id={station_id}"
        f"&bday={bday}"
        f"&fday={fday}"
        f"&amonth={month}"
        f"&ayear={year}"
        f"&bot=2"
    )


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )
    return s


def fetch_html(session: requests.Session, url: str) -> str:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"    запрос {attempt}/{MAX_RETRIES}: {url}")
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_error = e
            print(f"    -> ошибка запроса: {e}")
            time.sleep(2)

    raise last_error


# =========================
# ПАРСИНГ HTML
# =========================
def station_page_missing(html: str) -> bool:
    txt = html.lower()
    bad_markers = [
        "ничего не найдено",
        "станция не найдена",
        "нет данных",
        "404",
    ]
    return any(marker in txt for marker in bad_markers)


def parse_left_time_table(left_table) -> list[dict]:
    rows = left_table.select("tr")
    result = []

    for tr in rows[1:]:
        cells = tr.select("td")
        if len(cells) < 2:
            continue

        utc_time = normalize_text(cells[0].get_text(" ", strip=True))
        raw_day_month = normalize_text(cells[1].get_text(" ", strip=True))

        result.append(
            {
                "utc_time": utc_time,
                "day_month": raw_day_month,
            }
        )

    return result


def parse_main_table(main_table) -> tuple[list[str], list[list[str]]]:
    rows = main_table.select("tr")
    if not rows:
        return [], []

    header_cells = rows[0].select("td")
    raw_headers = [normalize_text(td.get_text(" ", strip=True)) for td in header_cells]

    # Разворачиваем "Ветер (напр.,м/с)" в 2 колонки
    headers = []
    for h in raw_headers:
        if h.startswith("Ветер"):
            headers.extend(["Ветер_направление", "Ветер_скорость_м_с"])
        else:
            headers.append(h)

    data_rows = []
    for tr in rows[1:]:
        cells = tr.select("td")
        row = [normalize_text(td.get_text(" ", strip=True)) for td in cells]
        data_rows.append(row)

    return headers, data_rows


def safe_headers(headers: list[str]) -> list[str]:
    cleaned = []
    used = {}

    for i, h in enumerate(headers):
        name = h if h else f"col_{i}"
        name = name.replace("\n", " ").strip()

        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1

        cleaned.append(name)

    return cleaned


def parse_day_month(day_month_str: str, year: int) -> str:
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\s*$", day_month_str)
    if not m:
        return ""
    day = int(m.group(1))
    month = int(m.group(2))
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_archive_html(html: str, station_id: str, year: int, month: int, url: str) -> tuple[pd.DataFrame, str]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("h1.chronicle-title")
    page_title = normalize_text(title_tag.get_text(" ", strip=True)) if title_tag else ""

    wrap = soup.select_one("div.archive-table")
    if not wrap:
        return pd.DataFrame(), page_title

    left_table = wrap.select_one("div.archive-table-left-column table")
    main_table = wrap.select_one("div.archive-table-wrap table")

    if left_table is None or main_table is None:
        return pd.DataFrame(), page_title

    left_rows = parse_left_time_table(left_table)
    headers, right_rows = parse_main_table(main_table)

    if not headers or not right_rows:
        return pd.DataFrame(), page_title

    headers = safe_headers(headers)

    if len(left_rows) != len(right_rows):
        raise ValueError(
            f"Число строк не совпадает: слева={len(left_rows)}, справа={len(right_rows)}"
        )

    merged_rows = []
    for left, right in zip(left_rows, right_rows):
        row = {
            "station_id": station_id,
            "page_title": page_title,
            "source_url": url,
            "year": year,
            "month": month,
            "date_utc": parse_day_month(left["day_month"], year),
            "time_utc": pd.to_numeric(left["utc_time"], errors="coerce"),
            "day_month_raw": left["day_month"],
        }

        for idx, header in enumerate(headers):
            row[header] = right[idx] if idx < len(right) else ""

        merged_rows.append(row)

    return pd.DataFrame(merged_rows), page_title

def process_stations_parallel(stations_df: pd.DataFrame, base_output_dir: Path, log=None, stop_event=None):
    total = len(stations_df)

    def write_log(message: str):
        print(message)
        if log:
            try:
                log(message)
            except Exception:
                pass

    futures = {}
    completed_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for _, row in stations_df.iterrows():
            if stop_event and stop_event.is_set():
                write_log("Парсинг остановлен пользователем до запуска всех задач.")
                break

            station_id = str(row["wmo_id"]).strip()
            station_name = str(row["station_name"]).strip()

            future = executor.submit(
                process_station_worker,
                station_id,
                station_name,
                base_output_dir,
                log,
                stop_event,
            )
            futures[future] = (station_id, station_name)

        for future in as_completed(futures):
            station_id, station_name = futures[future]
            completed_count += 1

            if stop_event and stop_event.is_set():
                write_log("Парсинг остановлен пользователем. Ожидаем завершения активных потоков.")

            try:
                result = future.result()
                status = "обновлено" if result else "без изменений/ошибка"
                write_log(f"[{completed_count}/{total}] {station_id} | {station_name} -> {status}")
            except Exception as e:
                write_log(f"[{completed_count}/{total}] {station_id} | {station_name} -> ошибка: {e}")
# =========================
# ОБРАБОТКА СТАНЦИИ
# =========================
def process_station(
    session: requests.Session,
    station_id: str,
    station_name: str,
    base_output_dir: Path,
    log=None,
    stop_event=None,
) -> bool:
    def write_log(message: str):
        print(message)
        if log:
            try:
                log(message)
            except Exception:
                pass

    write_log(f"\n=== Станция {station_id} | {station_name or 'без названия'} ===")

    if stop_event and stop_event.is_set():
        write_log(f"[{station_id}] Остановлено пользователем до начала обработки станции.")
        return False

    station_output_dir = base_output_dir / station_id
    station_output_dir.mkdir(parents=True, exist_ok=True)

    final_path = station_output_dir / f"{station_id}_ALL.csv"
    failed_path = station_output_dir / f"{station_id}_FAILED_MONTHS.txt"
    missing_path = station_output_dir / f"{station_id}_STATION_MISSING.txt"

    meta = load_station_meta(station_output_dir, station_id)

    requested_months = get_requested_months(START_DATE, END_DATE)
    present_months = set(meta.get("months_present", []))

    last_requested_month = requested_months[-1] if requested_months else None

    months_to_parse = []
    months_already_present = []

    for m in requested_months:
        if m == last_requested_month:
            months_to_parse.append(m)
        elif m not in present_months:
            months_to_parse.append(m)
        else:
            months_already_present.append(m)

    if months_already_present:
        write_log(
            f"[{station_id}] Уже есть месяцев в диапазоне: {len(months_already_present)}"
        )

    if not months_to_parse:
        write_log(f"[{station_id}] В выбранном диапазоне все месяцы уже есть. Ничего догружать не нужно.")
        return True

    existing_df = None
    if final_path.exists():
        try:
            existing_df = pd.read_csv(final_path, encoding="utf-8-sig", low_memory=False)
            existing_df = normalize_for_merge(existing_df)
        except Exception as e:
            write_log(f"[{station_id}] Не удалось прочитать существующий ALL.csv: {e}")

    all_parts = []
    failed_months = []
    missing_station = False
    rows_added = 0

    months_to_parse_set = set(months_to_parse)

    for year, month in month_iter(START_DATE, END_DATE):
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])

        if month_end < START_DATE or month_start > END_DATE:
            continue

        mkey = month_key(year, month)
        if mkey not in months_to_parse_set:
            continue

        if stop_event and stop_event.is_set():
            write_log(f"[{station_id}] Остановлено пользователем.")
            return False

        url = build_url(station_id, year, month)
        write_log(f"[{station_id}] {mkey}")

        try:
            html = fetch_html(session, url)

            if stop_event and stop_event.is_set():
                write_log(f"[{station_id}] Остановлено пользователем после загрузки страницы.")
                return False

            if station_page_missing(html):
                write_log("  -> станция не найдена или архив недоступен")
                failed_months.append(f"{mkey} | station_missing")
                missing_station = True
                continue

            df_month, page_title = parse_archive_html(html, station_id, year, month, url)

            if df_month.empty:
                write_log("  -> пустая таблица за месяц")
                failed_months.append(f"{mkey} | empty_table")

                sleep_left = SLEEP_BETWEEN_REQUESTS
                while sleep_left > 0:
                    if stop_event and stop_event.is_set():
                        write_log(f"[{station_id}] Остановлено пользователем во время ожидания.")
                        return False
                    chunk = min(0.2, sleep_left)
                    time.sleep(chunk)
                    sleep_left -= chunk
                continue

            df_month["station_name_pdf"] = station_name
            all_parts.append(df_month)
            rows_added += len(df_month)
            write_log(f"  -> строк: {len(df_month)} | месяц будет добавлен")

        except Exception as e:
            write_log(f"  -> ошибка: {e}")
            failed_months.append(f"{mkey} | {e}")

        sleep_left = SLEEP_BETWEEN_REQUESTS
        while sleep_left > 0:
            if stop_event and stop_event.is_set():
                write_log(f"[{station_id}] Остановлено пользователем во время ожидания.")
                return False
            chunk = min(0.2, sleep_left)
            time.sleep(chunk)
            sleep_left -= chunk

    final_df = None
    rows_added = 0
    removed = 0

    if all_parts:
        new_df = pd.concat(all_parts, ignore_index=True)
        new_df = normalize_for_merge(new_df)
        rows_added = len(new_df)

        if existing_df is not None and not existing_df.empty:
            final_df = pd.concat([existing_df, new_df], ignore_index=True)
        else:
            final_df = new_df
    elif existing_df is not None and not existing_df.empty:
        final_df = existing_df.copy()

    if final_df is not None and not final_df.empty:
        final_df = normalize_for_merge(final_df)

        dedupe_keys = [c for c in ["station_id", "date_utc", "time_utc"] if c in final_df.columns]
        before = len(final_df)

        if dedupe_keys:
            final_df = final_df.drop_duplicates(subset=dedupe_keys, keep="last")
        else:
            final_df = final_df.drop_duplicates(keep="last")

        removed = before - len(final_df)

        sort_keys = [c for c in ["date_utc", "time_utc"] if c in final_df.columns]
        if sort_keys:
            final_df = final_df.sort_values(by=sort_keys).reset_index(drop=True)
        else:
            final_df = final_df.reset_index(drop=True)

        final_df.to_csv(final_path, index=False, encoding="utf-8-sig")

        meta_end_date = date.today() - timedelta(days=1)
        new_meta = rebuild_station_meta_from_df(
            station_id=station_id,
            station_name=station_name,
            final_df=final_df,
            expected_start_date=date(2021, 3, 1),
            expected_end_date=meta_end_date,
        )
        save_station_meta(station_output_dir, station_id, new_meta)

        write_log(
            f"[{station_id}] Файл обновлён: {final_path} | "
            f"добавлено строк: {rows_added} | удалено дублей: {removed} | итог строк: {len(final_df)}"
        )
    else:
        write_log(f"[{station_id}] Новых данных для добавления не получено.")

    if failed_months:
        failed_path.write_text("\n".join(failed_months), encoding="utf-8")
        write_log(f"[{station_id}] Файл ошибок: {failed_path}")
    elif failed_path.exists():
        failed_path.unlink()

    if missing_station:
        missing_path.write_text(
            f"Станция {station_id} не найдена или недоступна на сайте.",
            encoding="utf-8",
        )
        write_log(f"[{station_id}] Станция отмечена как отсутствующая: {missing_path}")
    elif missing_path.exists():
        missing_path.unlink()

    return True


# =========================
# MAIN
# =========================
def main():
    ensure_output_dir(OUTPUT_DIR)

    stations_df = load_stations_from_csv(STATIONS_CSV_PATH)

    if TARGET_STATION_IDS is not None:
        stations_df = stations_df[stations_df["wmo_id"].isin(TARGET_STATION_IDS)]

    if stations_df.empty:
        raise ValueError("Станции для обработки не найдены.")

    if MULTITHREAD_ENABLED:
        print(f"Включён многопоточный режим: {MAX_WORKERS} потоков.")
        process_stations_parallel(
            stations_df=stations_df,
            base_output_dir=OUTPUT_DIR,
        )
    else:
        for _, row in stations_df.iterrows():
            station_id = str(row["wmo_id"]).strip()
            station_name = str(row["station_name"]).strip()

            process_station_worker(
                station_id=station_id,
                station_name=station_name,
                base_output_dir=OUTPUT_DIR,
            )

def run_parse_job(station_ids, start_date, end_date, output_dir=None, log=None, stop_event=None):
    global START_DATE, END_DATE, TARGET_STATION_IDS

    old_start = START_DATE
    old_end = END_DATE
    old_targets = TARGET_STATION_IDS

    base_output_dir = Path(output_dir) if output_dir else OUTPUT_DIR

    def write_log(message: str):
        print(message)
        if log:
            try:
                log(message)
            except Exception:
                pass

    try:
        START_DATE = start_date
        END_DATE = end_date
        TARGET_STATION_IDS = station_ids

        ensure_output_dir(base_output_dir)

        write_log(
            f"Запуск парсинга: станции={station_ids or 'все из CSV'}, "
            f"период={START_DATE}..{END_DATE}, папка={base_output_dir}"
        )

        stations_df = load_stations_from_csv(STATIONS_CSV_PATH)
        if TARGET_STATION_IDS is not None:
            stations_df = stations_df[stations_df["wmo_id"].isin(TARGET_STATION_IDS)]

        if stations_df.empty:
            raise ValueError("Станции для обработки не найдены.")

        if MULTITHREAD_ENABLED:
            write_log(f"Включён многопоточный режим: {MAX_WORKERS} потоков.")
            process_stations_parallel(
                stations_df=stations_df,
                base_output_dir=base_output_dir,
                log=write_log,
                stop_event=stop_event,
            )
        else:
            total = len(stations_df)
            for idx, (_, row) in enumerate(stations_df.iterrows(), start=1):
                if stop_event and stop_event.is_set():
                    write_log("Парсинг остановлен пользователем. Данные могут быть сохранены частично.")
                    return

                station_id = str(row["wmo_id"]).strip()
                station_name = str(row["station_name"]).strip()

                write_log(f"[{idx}/{total}] Станция {station_id} | {station_name}")

                completed = process_station_worker(
                    station_id=station_id,
                    station_name=station_name,
                    base_output_dir=base_output_dir,
                    log=write_log,
                    stop_event=stop_event,
                )

                if not completed:
                    write_log("Парсинг остановлен пользователем. Данные могут быть сохранены частично.")
                    return

        write_log("Парсинг завершён.")

    finally:
        START_DATE = old_start
        END_DATE = old_end
        TARGET_STATION_IDS = old_targets
if __name__ == "__main__":
    main()