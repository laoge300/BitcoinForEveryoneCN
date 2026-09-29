#!/usr/bin/env python3
"""
BitcoinForEveryoneCN - BTC 定投历史回测

固定口径：
- 数据源：Binance 公共市场数据 API
- 交易对：BTCUSDT（现货）
- K 线：1d
- 时区：UTC
- 买入价格：每月 10 日的 UTC 日线收盘价
- 默认每月投入：100 USD
- 默认不计手续费、税费和汇率
- 不使用备用数据源；请求失败就直接报错，避免悄悄换口径

官方公共市场数据端点：
https://data-api.binance.vision/api/v3/klines
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1d"
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Candle:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def to_ms(d: date, end_of_day: bool = False) -> int:
    t = time(23, 59, 59, 999000) if end_of_day else time(0, 0, 0)
    dt = datetime.combine(d, t, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_klines(start: date, end: date) -> list[Candle]:
    """只从 Binance 公共市场数据 API 获取数据；不做 fallback。"""
    start_ms = to_ms(start)
    end_ms = to_ms(end, end_of_day=True)
    rows: list[Candle] = []

    while start_ms <= end_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        url = API_BASE + "?" + urlencode(params)
        req = Request(url, headers={"User-Agent": "BitcoinForEveryoneCN/1.0"})

        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "无法从 Binance 公共市场数据 API 获取数据。"
                "为避免偷偷切换数据源，程序已停止。"
            ) from exc

        if not isinstance(payload, list):
            raise RuntimeError(f"Binance 返回异常：{payload!r}")
        if not payload:
            break

        for k in payload:
            open_time_ms = int(k[0])
            d = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()
            if start <= d <= end:
                rows.append(
                    Candle(
                        day=d,
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]),
                    )
                )

        last_open_ms = int(payload[-1][0])
        next_start = last_open_ms + 1
        if next_start <= start_ms:
            raise RuntimeError("分页游标没有前进，程序停止。")
        start_ms = next_start

        if len(payload) < 1000:
            break

    rows.sort(key=lambda x: x.day)

    # 防止 API 意外重复
    unique: dict[date, Candle] = {c.day: c for c in rows}
    rows = [unique[d] for d in sorted(unique)]

    if not rows:
        raise RuntimeError("没有取得任何价格数据。")

    return rows


def money(x: float) -> str:
    return f"${x:,.2f}"


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def run_backtest(
    candles: Iterable[Candle],
    start: date,
    end: date,
    monthly_usd: float,
    buy_day: int,
):
    candles = [c for c in candles if start <= c.day <= end]
    by_day = {c.day: c for c in candles}

    if start not in by_day:
        raise RuntimeError(f"起始日 {start} 没有日线数据。")
    if end not in by_day:
        raise RuntimeError(f"结束日 {end} 没有完整日线数据。")

    btc = 0.0
    invested = 0.0
    purchases: list[dict] = []

    worst_return = 0.0
    worst_day: date | None = None
    worst_value = 0.0
    worst_invested = 0.0

    underwater_start: date | None = None
    longest_underwater_days = 0
    longest_underwater_start: date | None = None
    longest_underwater_end: date | None = None

    daily_rows: list[dict] = []

    for c in candles:
        # 在当天日线收盘价买入，因此先完成当天买入，再按同一收盘价估值。
        if c.day.day == buy_day:
            qty = monthly_usd / c.close
            btc += qty
            invested += monthly_usd
            purchases.append(
                {
                    "date": c.day.isoformat(),
                    "close": c.close,
                    "usd": monthly_usd,
                    "btc_bought": qty,
                    "btc_total": btc,
                    "invested_total": invested,
                }
            )

        if invested <= 0:
            continue

        value = btc * c.close
        ret = value / invested - 1.0

        daily_rows.append(
            {
                "date": c.day.isoformat(),
                "close": c.close,
                "invested_total": invested,
                "btc_total": btc,
                "portfolio_value": value,
                "return_vs_invested": ret,
            }
        )

        if ret < worst_return:
            worst_return = ret
            worst_day = c.day
            worst_value = value
            worst_invested = invested

        if ret < 0:
            if underwater_start is None:
                underwater_start = c.day
        else:
            if underwater_start is not None:
                duration = (c.day - underwater_start).days
                if duration > longest_underwater_days:
                    longest_underwater_days = duration
                    longest_underwater_start = underwater_start
                    longest_underwater_end = c.day
                underwater_start = None

    # 如果到结束日仍未回到累计投入之上
    if underwater_start is not None:
        duration = (end - underwater_start).days + 1
        if duration > longest_underwater_days:
            longest_underwater_days = duration
            longest_underwater_start = underwater_start
            longest_underwater_end = end

    if not purchases:
        raise RuntimeError("指定区间内没有发生任何定投。")

    final_close = by_day[end].close
    final_value = btc * final_close
    avg_cost = invested / btc
    total_return = final_value / invested - 1.0

    # 一次性买入仅作为反事实对照：
    # 前提是全部定投资金在第一天已经存在。
    start_close = by_day[start].close
    lump_sum_btc = invested / start_close
    lump_sum_value = lump_sum_btc * final_close
    lump_sum_return = lump_sum_value / invested - 1.0

    result = {
        "source": "Binance public market data API",
        "endpoint": API_BASE,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "timezone": "UTC",
        "price_field": "daily close",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "buy_day": buy_day,
        "monthly_usd": monthly_usd,
        "fees_included": False,
        "taxes_included": False,
        "fx_included": False,
        "purchase_count": len(purchases),
        "total_invested_usd": invested,
        "total_btc": btc,
        "average_cost_usd_per_btc": avg_cost,
        "final_close_usd": final_close,
        "final_value_usd": final_value,
        "return_vs_invested": total_return,
        "worst_return_vs_invested": worst_return,
        "worst_day": worst_day.isoformat() if worst_day else None,
        "worst_portfolio_value_usd": worst_value,
        "worst_invested_usd": worst_invested,
        "longest_underwater_days": longest_underwater_days,
        "longest_underwater_start": (
            longest_underwater_start.isoformat()
            if longest_underwater_start
            else None
        ),
        "longest_underwater_end": (
            longest_underwater_end.isoformat()
            if longest_underwater_end
            else None
        ),
        "lump_sum_assumption": (
            "Only comparable if the entire future DCA capital already existed on the start date."
        ),
        "lump_sum_btc": lump_sum_btc,
        "lump_sum_value_usd": lump_sum_value,
        "lump_sum_return": lump_sum_return,
    }
    return result, purchases, daily_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"""# BTC 定投回测结果

- 数据源：Binance 公共市场数据 API
- 交易对：{result['symbol']}（现货）
- K 线：{result['interval']}
- 时区：{result['timezone']}
- 买入价格：UTC 日线收盘价
- 开始日期：{result['start_date']}
- 结束日期：{result['end_date']}
- 买入规则：每月 {result['buy_day']} 日固定投入 {money(result['monthly_usd'])}
- 手续费：未计入
- 税费：未计入
- 汇率：未计入

## 结果

| 项目 | 结果 |
|---|---:|
| 定投次数 | {result['purchase_count']} 次 |
| 累计投入 | {money(result['total_invested_usd'])} |
| 累计 BTC | {result['total_btc']:.9f} BTC |
| 平均持有成本 | {money(result['average_cost_usd_per_btc'])} / BTC |
| 结束日收盘价 | {money(result['final_close_usd'])} |
| 结束日持仓价值 | {money(result['final_value_usd'])} |
| 相对累计投入 | {pct(result['return_vs_invested'])} |
| 最差相对累计投入浮亏 | {pct(result['worst_return_vs_invested'])} |
| 最差日期 | {result['worst_day']} |
| 最长低于累计投入 | {result['longest_underwater_days']} 天 |

## 一次性买入对照

这个对照只有在“全部未来定投资金在第一天已经存在”时才公平。

- 第一天一次性投入：{money(result['total_invested_usd'])}
- 获得 BTC：{result['lump_sum_btc']:.9f} BTC
- 结束日价值：{money(result['lump_sum_value_usd'])}
- 相对投入：{pct(result['lump_sum_return'])}

## 重要限制

这是一段历史回测，不是未来收益预测。
历史表现不能保证未来结果。
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="BTC monthly DCA historical backtest")
    p.add_argument("--start", default="2021-11-10")
    p.add_argument("--end", default="2026-09-27")
    p.add_argument("--monthly-usd", type=float, default=100.0)
    p.add_argument("--buy-day", type=int, default=10)
    p.add_argument("--output-dir", default="output/dca-from-ath")
    args = p.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    if end < start:
        raise SystemExit("结束日期不能早于开始日期。")
    if not 1 <= args.buy_day <= 28:
        raise SystemExit("为避免月底缺日问题，buy-day 只允许 1 到 28。")
    if args.monthly_usd <= 0:
        raise SystemExit("monthly-usd 必须大于 0。")

    candles = fetch_klines(start, end)
    result, purchases, daily_rows = run_backtest(
        candles, start, end, args.monthly_usd, args.buy_day
    )

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(out / "purchases.csv", purchases)
    write_csv(out / "daily_portfolio.csv", daily_rows)
    write_markdown(out / "summary.md", result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成：{out / 'summary.md'}")
    print(f"已生成：{out / 'summary.json'}")
    print(f"已生成：{out / 'purchases.csv'}")
    print(f"已生成：{out / 'daily_portfolio.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
