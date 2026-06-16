# Capital — Portfolio & Budget

A cool, single-file web app that tracks your **investment portfolio** and turns it
into a **monthly budget** you can spend against. No build step, no server, no
account — just open `index.html`. All data is stored locally on your device.

## Features

### 📈 Portfolio
- Add stocks, funds, crypto, or cash positions (symbol, quantity, avg cost, current price)
- Live net-worth hero with all-time unrealized gain/loss and cost basis
- Donut allocation chart + per-holding gain/loss
- Edit or delete any holding; update a price to refresh value

### 💸 Budget (based off your portfolio)
- Monthly budget is derived from your portfolio via an **annual draw rate**
  (the classic 4% rule) — so as your investments grow, your budget grows.
  Or switch to a fixed monthly amount.
- Split the budget into categories (each a % of the monthly budget)
- Log expenses and track spent vs allocated per category, with month-by-month history
- See what's left to spend, over-budget warnings, and annual draw

### ✨ Polish
- Dark, gradient UI with animated progress bars and a glassy bottom tab bar
- Installable as a PWA (Add to Home Screen) with a generated app icon
- Works fully offline; export your data to JSON anytime

## Run it
Open `index.html` in any browser, or serve the folder:

```bash
python3 -m http.server 8000   # then visit http://localhost:8000
```
