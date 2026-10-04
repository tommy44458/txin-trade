# Changelog

The product version is managed in `version.json`. Dates record release preparation; public availability is determined by the official download page. Each item is one sentence.

## [Unreleased]

### Changed

- Without a cloud account, the bottom left suggests signing in to use txinTrade remotely from a phone.

### Fixed

- A report's reconciliation result sits in its own small card instead of against the analysis below.

## [1.1.2] - 2026-10-04

### Changed

- Entry plans include a checkable invalidation rule, and reconciliation settles on whichever comes first: target, stop or invalidation.

### Fixed

- On Windows, an update found while the app starts is now offered once the app has loaded.

## [1.1.1] - 2026-10-04

### Fixed

- AI performance on phones uses a more compact layout, keeps switches on one line, and lists only checkable results.

## [1.1.0] - 2026-10-04

### Added

- After a report is made, the app reconciles it in the background on later 5-minute candles, recording wins, losses and R.
- A new AI performance page compares results by pair, timeframe, risk tolerance, model and prompt version.
- Settings can share reconciled results to improve the AI; it is off by default and sends only results and versions.
- Waiting entry plans include a checkable trigger rule, such as a close above a price or a trade at a price.

### Changed

- While an analysis runs, it shows each finished step, the price and nearby levels, and the AI sections already written.

## [1.0.12] - 2026-10-04

### Fixed

- The remote web page can run AI analyses with the ChatGPT plan, Claude API or OpenAI API too.
- Closing the Windows app no longer sometimes shows a JavaScript error window.

## [1.0.11] - 2026-10-04

### Changed

- With a high risk tolerance, market and position analyses use a wider stop and farther take-profit targets.

## [1.0.10] - 2026-10-04

### Fixed

- Choosing the ChatGPT plan without Codex installed now explains it in Settings, with install steps.

## [1.0.9] - 2026-10-04

### Added

- When a position analysis recommends holding, it now includes an exit plan with the price that ends the hold, a suggested stop, and take-profit levels.
- New ChatGPT plan (official) option uses your Plus or Pro plan through OpenAI's official Sign in with ChatGPT.
- New Claude API (API key) option uses Claude with your own Anthropic API key, billed per use to your account.
- New OpenAI API (API key) option analyzes with your own OpenAI key and lists the models it can use.

### Changed

- Choosing Claude Code or Codex in Settings now explains how that connection relates to the provider's terms.

## [1.0.8] - 2026-10-03

### Added

- txinTrade is now available for Windows (Windows 10 or 11, 64-bit), with the same in-app updates.

### Changed

- Claude Code and Codex setup now walks through each step with commands for your computer's system, each copied in one click.

## [1.0.7] - 2026-10-03

### Changed

- Fund-flow charts show each period's inflow and outflow on hover or tap, and large movements are paged ten at a time.

### Fixed

- When your data comes from a newer version, the startup screen says so and offers the update instead of a generic failure.
- Updates are still checked and can be installed when the local service fails to start.

## [1.0.6] - 2026-10-03

### Added

- A new Fund flows page follows large exchange inflows and outflows, whale transfers and stablecoin supply.
- The AI answers follow-up questions on the Fund flows page, and market and position analyses consider fund flows.
- The remote web page offers Fund flows and its AI follow-ups too.

### Changed

- Traditional Chinese reports, follow-up replies and macro interpretations use full-width marks and a space between Chinese and English or numbers.

### Fixed

- The BLS calendar is read again, so CPI and jobs report dates show; its time zone name had stopped it.

## [1.0.5] - 2026-10-03

### Changed

- Traditional Chinese reports show full-width commas and semicolons next to Chinese text.
- The update window now opens inside the app, scrolls when the notes are long, and shows only your language.

## [1.0.4] - 2026-10-02

### Changed

- The AI no longer rules out the countertrend side just because the larger trend disagrees, rating it conditional and stating the cost instead.
- Follow-ups about a conditional side get a concrete plan, and the AI does not change its assessment because you disagree.

### Fixed

- After switching to another cloud account, Settings and the account at the bottom left now update correctly.

## [1.0.3] - 2026-10-02

### Changed

- Settings and the account menu show the remote web page address, ready to open or copy.
- The remote web page guides accounts with no connected computer through three steps to turn on remote access.

## [1.0.2] - 2026-10-02

### Changed

- While an update is available, a notice stays at the bottom left and shows download progress, as does the Dock icon.
- The app checks for updates within seconds of opening.

## [1.0.1] - 2026-10-02

### Changed

- Support and resistance in position analyses are listed by price, with the analysis-time price marked.

### Fixed

- The remote web page shows the signed-in account and a way to sign out even when the computer is offline.

## [1.0.0] - 2026-10-02

The first public release of txinTrade: crypto futures analysis on your computer, with the Claude Code or Codex you already use.

### Added

- Analyses consider the BTC and ETH market trend and how closely the pair moves with each.
- Switching pairs shows that pair's latest completed analysis.
- You can optionally sign in to a txinTrade cloud account, and local features work the same without it.
- With remote access on, any device's browser shows the same screens as the desktop.
- The chart can toggle each indicator the AI used.

### Changed

- Settings, remote access, and sign-out are gathered in the account menu at the bottom left.
- Phone widths use an iOS-style layout with the main pages in a bottom tab bar.
- Support and resistance are listed from the highest price down, with the current price marked.
- Positions are laid out in columns, and adding a position is easier to find.
- On phones the current price leads the market card, and secondary text is easier to read.
- Exchange sync fits in one row of chips, with a button to sync every exchange.
- The AI no longer sees your directional view and assesses long and short separately.

### Fixed

- Slightly malformed AI reports are repaired and displayed normally.
- The account menu is no longer covered by the price chart.
- Spacing between several sections of the positions page is fixed.
- A stop loss the exchange does not report is labelled as such, with a button to add one.
- Restarting the app quickly no longer fails with "the local backend did not start".

## [0.2.0] - 2026-10-02

### Added

- A single product version and a bilingual release notes workflow.
- About txinTrade and the changelog in Settings.
- macOS installers built and signed in GitHub Actions.
- In-app updates that back up your data before installing.
- Source code licensed under the GNU AGPL v3, with a contributor agreement and trademark policy.
- Claude Code as an AI analysis source.
- Settings detect whether Codex and Claude Code are installed and signed in.
- A txinTrade wordmark app icon.

### Changed

- The product is renamed txinTrade, and older data moves to the new data folder automatically.
- The startup page shows an animated txinTrade wordmark.
- The sidebar no longer shows the internal workspace ID.

### Removed

- The direct ChatGPT account source is removed in favor of Codex or Claude Code.
