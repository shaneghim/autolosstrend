"""Monthly update: pulls the latest data, recalculates trends, and redraws the charts."""
import os
import sqlite3
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
import matplotlib
#we need to draw charts to files, since the server has no screen
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


load_dotenv()
FRED_KEY = os.getenv("FRED_API_KEY")
BLS_KEY = os.getenv("BLS_API_KEY")
DB_PATH = "data/trend.db"

#1) Pull the data (notebook 1)

#five series from FRED
fred_series = {"CUSR0000SETD": "repair",
               "CUSR0000SETC": "parts",
               "CUSR0000SETA02": "used_cars",
               "CPIMEDSL": "medical",
               "TRFVOLUSM227SFWA": "miles_traveled"}

#response.raise_for_status(): stop if FRED returns an error
fred_url = "https://api.stlouisfed.org/fred/series/observations"
frames = []
for code, name in fred_series.items():
    params = {"series_id": code, "api_key": FRED_KEY, "file_type": "json"}
    response = requests.get(fred_url, params=params, timeout=30)
    response.raise_for_status()
    df = pd.DataFrame(response.json()["observations"])[["date", "value"]]
    df["series"] = name
    frames.append(df)

fred = pd.concat(frames, ignore_index=True)
fred["date"] = pd.to_datetime(fred["date"])
fred["value"] = pd.to_numeric(fred["value"], errors="coerce")

#motor vehicle insurance from BLS (FRED doesn't carry it), latest 16 years
#if bls_json[] ...: stop if BLS returns an error
#this_year - 15 keeps the request within BLS's 20-year limit, incuding current year
this_year = pd.Timestamp.today().year
payload = {"seriesid": ["CUSR0000SETE"],
           "startyear": str(this_year - 15),
           "endyear": str(this_year),
           "registrationkey": BLS_KEY}
bls_response = requests.post("https://api.bls.gov/publicAPI/v2/timeseries/data/", json=payload, timeout=30)
bls_json = bls_response.json()
if bls_json["status"] != "REQUEST_SUCCEEDED":
    raise RuntimeError(f"BLS request failed: {bls_json.get('message')}")

insurance = pd.DataFrame(bls_json["Results"]["series"][0]["data"])
insurance = insurance[insurance["period"] != "M13"]
insurance["date"] = pd.to_datetime(insurance["year"] + "-" + insurance["period"].str[1:] + "-01")
insurance["value"] = pd.to_numeric(insurance["value"], errors="coerce")
insurance["series"] = "insurance"
insurance = insurance[["date", "value", "series"]].sort_values("date")

#stack and save, replacing the table since BLS revises seasonally adjusted values each February
monthly = pd.concat([fred, insurance], ignore_index=True)
monthly["date"] = monthly["date"].dt.strftime("%Y-%m-%d")

conn = sqlite3.connect(DB_PATH)
monthly.to_sql("monthly_series", conn, if_exists="replace", index=False)

print("Data pulled and saved:")
print(monthly.groupby("series")["date"].agg(["count", "max"]))







#2) Calculate trends (notebook 2)

#12-month changes, matching on the date one year earlier (insurance has no row for November 2025)
yoy_query = """
SELECT cur.series,
       cur.date,
       cur.value,
       prev.value AS value_year_ago,
       cur.value / prev.value - 1 AS yoy_change
FROM monthly_series AS cur
LEFT JOIN monthly_series AS prev
  ON prev.series = cur.series
 AND prev.date = date(cur.date, '-1 year')
WHERE cur.date >= '2015-01-01'
ORDER BY cur.series, cur.date
"""
yoy = pd.read_sql(yoy_query, conn)

#monthly values for the trend fits, skipping blanks
monthly_query = """
SELECT series, date, value
FROM monthly_series
WHERE date >= '2015-01-01'
  AND value IS NOT NULL
"""
values = pd.read_sql(monthly_query, conn)
values["date"] = pd.to_datetime(values["date"])

#fits an exponential trend over the latest window and returns the annual trend rate
def annual_trend(group, years):
    end = group["date"].max()
    window = group[group["date"] > end - pd.DateOffset(years=years)]
    t = (window["date"] - end).dt.days / 365.25
    slope = np.polyfit(t, np.log(window["value"]), 1)[0]
    return np.exp(slope) - 1

rows = []
for name, group in values.groupby("series"):
    rows.append({"series": name,
                 "trend_3yr": annual_trend(group, 3),
                 "trend_5yr": annual_trend(group, 5)})
trends = pd.DataFrame(rows)

#weighted claim cost (severity) trend vs. premium trend
weights = {"repair": 0.31, "parts": 0.27, "used_cars": 0.17, "medical": 0.25}
trends["weight"] = trends["series"].map(weights)
severity_3yr = (trends["trend_3yr"] * trends["weight"]).sum()
severity_5yr = (trends["trend_5yr"] * trends["weight"]).sum()
premium = trends.set_index("series").loc["insurance"]

comparison = pd.DataFrame({
    "window": ["3 years", "5 years"],
    "severity_trend": [severity_3yr, severity_5yr],
    "premium_trend": [premium["trend_3yr"], premium["trend_5yr"]]})
comparison["gap"] = comparison["premium_trend"] - comparison["severity_trend"]

#trend factors: midpoint of the latest 12 months to the average claim date
#for 12-month policies written starting July 1, 2027 (July 1, 2028) = 2.25 years
trend_years = 2.25
comparison["severity_factor"] = (1 + comparison["severity_trend"]) ** trend_years
comparison["premium_factor"] = (1 + comparison["premium_trend"]) ** trend_years

#sensitivity test on the severity weights
scenarios = {"Base": {"repair": 0.31, "parts": 0.27, "used_cars": 0.17, "medical": 0.25},
             "More injury": {"repair": 0.27, "parts": 0.23, "used_cars": 0.15, "medical": 0.35},
             "More total loss": {"repair": 0.26, "parts": 0.22, "used_cars": 0.27, "medical": 0.25},
             "More parts": {"repair": 0.25, "parts": 0.33, "used_cars": 0.17, "medical": 0.25}}
rows = []
for name, w in scenarios.items():
    sev_3yr = (trends["trend_3yr"] * trends["series"].map(w)).sum()
    rows.append({"scenario": name,
                 "severity_3yr": sev_3yr,
                 "gap_3yr": premium["trend_3yr"] - sev_3yr})
sensitivity = pd.DataFrame(rows)

#save results
yoy.to_sql("yoy_changes", conn, if_exists="replace", index=False)
trends.to_sql("trend_rates", conn, if_exists="replace", index=False)
comparison.to_sql("severity_vs_premium", conn, if_exists="replace", index=False)
sensitivity.to_sql("sensitivity", conn, if_exists="replace", index=False)

print("\nTrends calculated:")
print(comparison[["window", "severity_trend", "premium_trend", "gap"]].round(4))







#3) Charts and Excel files (notebook 3)

#chart 1: premiums vs. claim costs, 12-month change since 2018
chart = yoy[yoy["date"] >= "2018-01-01"].copy()
chart["date"] = pd.to_datetime(chart["date"])
wide = chart.pivot(index="date", columns="series", values="yoy_change")
wide["severity"] = (wide[list(weights)] * pd.Series(weights)).sum(axis=1, min_count=4)
latest = wide[["insurance", "severity"]].dropna().index.max()

fig, ax = plt.subplots(figsize=(10, 5.5))
ax.plot(wide.index, wide["insurance"], color="#1f4e79", linewidth=2, label="Premiums (insurance CPI)")
ax.plot(wide.index, wide["severity"], color="#d95f02", linewidth=2, label="Claim costs (weighted index)")
ax.axhline(0, color="grey", linewidth=0.8)
ax.annotate("COVID premium refunds", xy=(pd.Timestamp("2020-05-01"), -0.143),
            xytext=(pd.Timestamp("2020-10-01"), -0.135), fontsize=9, va="center",
            arrowprops=dict(arrowstyle="->", color="grey"))
ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
ax.grid(axis="y", alpha=0.3)
ax.set_xlim(pd.Timestamp("2018-01-01"), wide.index.max() + pd.DateOffset(months=2))
ax.text(0, 1.08, "Auto insurance premiums vs. claim costs", transform=ax.transAxes, fontsize=14)
ax.set_title(f"12-month change, January 2018 to {latest:%B %Y}", loc="left", fontsize=10, color="grey", pad=8)
ax.set_ylabel("Change from a year earlier")
ax.legend(frameon=False, loc="upper left")
ax.spines[["top", "right"]].set_visible(False)
fig.text(0.01, -0.02, "Claim costs weight repair 31%, parts 27%, used cars 17%, and medical 25%. "
         "Gaps are October and November 2025, which BLS didn't publish because of the shutdown. Sources: BLS, FRED.",
         fontsize=8, color="grey")
fig.savefig("images/premium_vs_severity.png", dpi=200, bbox_inches="tight")
plt.close(fig)

#chart 2: fitted trend rates by series
claim_costs = {"series": "claim_costs", "trend_3yr": severity_3yr, "trend_5yr": severity_5yr}
rates = pd.concat([trends[["series", "trend_3yr", "trend_5yr"]], pd.DataFrame([claim_costs])], ignore_index=True)
labels = {"insurance": "Premiums (insurance)",
          "claim_costs": "Claim costs (weighted)",
          "repair": "Repair",
          "parts": "Parts",
          "medical": "Medical",
          "used_cars": "Used cars",
          "miles_traveled": "Miles traveled"}
plot_rates = rates.set_index("series").loc[list(labels)]

fig, ax = plt.subplots(figsize=(9, 5.5))
y = np.arange(len(plot_rates))
h = 0.38
bars3 = ax.barh(y - h/2, plot_rates["trend_3yr"], height=h, color="#1f4e79", label="3-year trend")
bars5 = ax.barh(y + h/2, plot_rates["trend_5yr"], height=h, color="#7fb3d5", label="5-year trend")
ax.bar_label(bars3, labels=[f"{v:.1%}" for v in plot_rates["trend_3yr"]], padding=3, fontsize=8)
ax.bar_label(bars5, labels=[f"{v:.1%}" for v in plot_rates["trend_5yr"]], padding=3, fontsize=8)
ax.set_yticks(y, [labels[s] for s in plot_rates.index])
ax.invert_yaxis()
ax.tick_params(axis="y", length=0)
ax.axvline(0, color="grey", linewidth=0.8)
for line in [1.5, 5.5]:
    ax.axhline(line, color="lightgrey", linewidth=0.8, linestyle="--")
ax.set_xlim(-0.05, 0.12)
ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
ax.grid(axis="x", alpha=0.3)
ax.set_axisbelow(True)
ax.text(0, 1.08, "Fitted annual trend rates", transform=ax.transAxes, fontsize=14)
ax.set_title("Annual growth rate by series, last 3 and 5 years", loc="left", fontsize=10, color="grey", pad=8)
ax.legend(frameon=False, loc="lower right")
ax.spines[["top", "right", "left"]].set_visible(False)
fig.text(0.01, -0.02, "Exponential trend fitted to monthly values over the latest 3 and 5 years. "
         "Claim costs weight repair 31%, parts 27%, used cars 17%, and medical 25%. Sources: BLS, FRED.",
         fontsize=8, color="grey")
fig.savefig("images/trend_rates.png", dpi=200, bbox_inches="tight")
plt.close(fig)

#CSV files for Excel (for Power Query)
os.makedirs("exports", exist_ok=True)
yoy.to_csv("exports/yoy_changes.csv", index=False)
rates.to_csv("exports/trend_rates.csv", index=False)
comparison.to_csv("exports/severity_vs_premium.csv", index=False)
sensitivity.to_csv("exports/sensitivity.csv", index=False)

conn.close()
print(f"\nCharts and Excel files updated. Latest month: {latest:%B %Y}")