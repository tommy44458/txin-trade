<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/logo-dark.png">
    <img src="assets/readme/logo-light.png" width="300" alt="txinTrade">
  </picture>
</p>

<p align="center">
  An AI trading analyst for crypto futures, powered by the Claude or ChatGPT subscription you already have.<br>
  It reads the market, explains what it sees, and tells you when to act — and when not to.
</p>

<p align="center">
  <b>English</b> · <a href="README.zh-TW.md">繁體中文</a>
</p>

![txinTrade market analysis](assets/readme/en/market.jpg)

txinTrade looks at a Binance USDT perpetual market the way a careful trader would: price action across several timeframes, support and resistance, the indicators that matter, U.S. economic data, and how BTC and ETH are moving. It then gives you a clear recommendation with the prices and conditions behind it.

**It never trades for you.** txinTrade only analyzes and, if you allow it, reads your positions. It cannot place orders, close positions, change leverage, or move funds.

## Bring your own AI

txinTrade integrates with the AI tools already installed and signed in on your computer: **Claude Code** or **Codex**. It hands them real market data and lets them do the analysis, on the **Claude** or **ChatGPT** plan you already use and with the most capable models it offers, such as Claude Opus.

- **Works with what you already have.** Choose Claude Code or Codex once in Settings; there is no new AI account to open.
- **Your AI, your plan.** Every analysis runs under your own sign-in, and txinTrade adds no AI fees.
- **Pick the model** you want for analysis in Settings.

Your sign-in stays with Claude Code or Codex; txinTrade never reads or stores those credentials.

## What txinTrade does for you

### Get a clear call, with a plan

Choose a market, a timeframe, your leverage, and how you like to trade: how much risk you accept, and whether you prefer to enter early near a key level or wait for confirmation. txinTrade comes back with a decision — enter now, wait for an entry, or stand aside — together with a reference entry, stop loss and take profit, the conditions to wait for, and what would prove the plan wrong.

![An AI recommendation with entry, stop loss, take profit and conditions](assets/readme/en/report.jpg)

### Both directions, judged on the evidence

Every analysis weighs long and short separately and says which one is reasonable now, which one needs conditions, and which one to avoid. You can tell txinTrade which way you lean, but the AI never sees it, so your view cannot tilt its judgment. If your direction is the one it finds unsuitable, the app shows you that first.

### See the reasons on the chart

The chart shows the support and resistance zones from the analysis, where the current price sits among them, and every indicator the AI used — moving averages, VWAP, Bollinger Bands, Fibonacci, RSI, MACD and more. Turn each one on or off to check the reasoning yourself. Past reports keep the chart exactly as it was at the time.

![Chart with support and resistance zones and the indicators the AI used](assets/readme/en/chart.jpg)

### Advice on the positions you already hold

Import your open positions from Binance or BingX with a read-only key, or enter them by hand. Select one or more and txinTrade tells you whether to hold or close, why, and what would change its mind, with risk estimates for reference. Every hold comes with an exit plan: the price that ends the hold, a suggested stop, and where to take profit.

![Your open positions, ready for analysis](assets/readme/en/positions.jpg)

![A hold-or-close recommendation for an open short position](assets/readme/en/position-report.jpg)

### Economic news, explained in plain words

txinTrade collects official U.S. data — CPI, jobs, PCE, GDP and Federal Reserve decisions — and explains what it means for crypto, with a link to every source. The same interpretation feeds into each trading analysis, so the macro picture is always part of the call.

![AI interpretation of the latest U.S. economic data](assets/readme/en/macro.jpg)

### Ask follow-up questions

Not sure about a call? Ask. The AI chat answers about that specific analysis — what would change the view, where a better entry might be, what the risks are — and checks the latest price each time you ask. The original report stays unchanged.

![Asking the AI a follow-up question about an analysis](assets/readme/en/chat.jpg)

### Every analysis, kept

Your history keeps each report with the market data, settings and reasoning it was based on, so you can look back at what the AI said and why.

![Analysis history](assets/readme/en/history.jpg)

### Use it from your phone

With an optional txinTrade cloud account, you can open the same screens from your phone or any browser at [app.txintrade.com](https://app.txintrade.com): check your analyses and positions, start a new analysis, and follow up with the AI. The analysis still runs on your computer, and your keys and data stay there; the cloud only passes your requests along. Remote access is a paid service that is currently in preview, and your computer needs to be on with txinTrade open.

<p align="center">
  <img src="assets/readme/en/mobile-market.jpg" width="300" alt="txinTrade on a phone: market analysis">
  &nbsp;&nbsp;
  <img src="assets/readme/en/mobile-report.jpg" width="300" alt="txinTrade on a phone: an AI recommendation">
</p>

## Your data stays yours

- **Runs on your computer.** Your positions, preferences, history and conversations are saved locally. There is no txinTrade account to create unless you want remote access.
- **Uses your own AI account.** Analysis runs through the Claude Code or Codex sign-in you already have. The market, position and conversation details of each analysis are sent to that AI provider.
- **Read-only exchange access.** Exchange keys only need read permission, and txinTrade never asks for trading, transfer or withdrawal rights. Keys are stored encrypted on your computer.
- **Honest about limits.** If the AI or a data source fails, the app says so instead of filling the gap. AI analysis is not financial advice, and past recommendations do not guarantee future results.

## Getting started

txinTrade runs on **macOS with Apple Silicon** (M1 or later) and on **64-bit Windows 10 or 11**.

1. [Download txinTrade](https://txintrade.com/download) (or from the [latest release](https://github.com/tommy44458/txin-trade/releases/latest)).
   - **Mac:** open the DMG and drag txinTrade to Applications. The app is signed and notarized by Apple.
   - **Windows:** run the installer. It is not code-signed yet, so Windows may say it protected your PC: choose **More info**, then **Run anyway**.
2. Open txinTrade. Your data is created automatically; there is no database or configuration file to set up.
3. In **Settings**, connect the AI subscription you already have:
   - **Codex** — uses the Codex app you have installed and its sign-in.
   - **Claude Code** — sign in once with `claude auth login` in a terminal, then choose **Connect Claude Code**. txinTrade only checks that you are signed in and never reads your Claude credentials.

Keep txinTrade open while an analysis is running; closing it stops the work in progress. txinTrade checks for updates and installs them for you, after backing up your data. To run it from the source code instead, see [CONTRIBUTING.md](CONTRIBUTING.md).

### Optional connections

Add these in **Settings** when you want them:

| Connection | What it adds |
| --- | --- |
| Binance (read-only key) | Imports your USDⓈ-M USDT perpetual positions. |
| BingX (read-only key) | Imports supported perpetual and Standard Futures positions. |
| Jev | Classifies official economic news in the background. |
| JBlanked | Cross-checks the economic calendar. |

Create exchange keys with **read-only** permission only. Binance import has automated test coverage but has not yet been confirmed with a real read-only account.

### Languages and appearance

Use txinTrade in English or Traditional Chinese, in light or dark mode, or following your system. Each report keeps the language it was written in.

## Frequently asked questions

**Does txinTrade trade for me?**
No. It analyzes markets and positions and gives recommendations. You decide and place every trade yourself.

**Which markets does it cover?**
All currently tradable Binance USDT perpetual futures. Coin-margined, USDC and delivery contracts are not included.

**What does it cost?**
The app is free and open source. Bring your own AI: analyses run through the Claude Code or Codex already signed in on your computer, on the plan you already have, and txinTrade adds no AI fees. Remote access from your phone is an optional paid cloud service.

**Can I trust the recommendations?**
Treat them as a well-reasoned second opinion, not a guarantee. Every report shows the evidence and the conditions that would invalidate it, so you can judge it yourself.

## For developers

Building from source, the project structure, tests and the release process are described in [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and focused pull requests are welcome.

## License

txinTrade is licensed under the [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). You may use, study, modify and share it. If you distribute a modified version, or let others use one over a network, you must make its complete source code available under the same license. Commercial licenses without these obligations are available from the copyright holder.

Contributions require accepting the [Contributor License Agreement](CLA.md). The txinTrade name and logo are not covered by the AGPL; see the [trademark policy](TRADEMARKS.md). Third-party attributions are listed in [NOTICE](NOTICE).
