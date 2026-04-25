#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dr. Stonk Financial Terminology Education System.

Provides footnoted explanations for financial terms in portfolio output.
Builds the "Dr. Stonk Box" - an educational footer with term definitions.
"""

import logging
import os
from typing import Dict, Set

logger = logging.getLogger(__name__)

# Financial term explanations — Dr. Stonk from the planet Hephaestus
#
# Two formats supported for backward compatibility:
#   - str: legacy single-paragraph explanation
#   - dict: {"definition", "why_it_matters", "example", "red_flag"}
#
# The consumer (build_dr_stonk_box) handles both shapes.
TERM_EXPLANATIONS = {
    # ---------- Risk / Return metrics ----------
    "Sharpe Ratio": {
        "definition": "Risk-adjusted return: how much excess return you earn per unit of volatility taken.",
        "why_it_matters": "Lets you compare portfolios or funds on a level playing field, net of risk.",
        "example": "Portfolio returns 10%, risk-free rate 4%, volatility 12% → Sharpe = (10-4)/12 = 0.5. Values >1 are good, >2 excellent.",
        "red_flag": "A single year of high Sharpe can be luck. Look for 3-5 year consistency.",
    },
    "Volatility": {
        "definition": "A statistical measure of how much returns fluctuate around their average — the standard deviation of returns.",
        "why_it_matters": "Higher volatility = bigger swings, which affects both upside and drawdown risk.",
        "example": "A fund with 18% annual volatility typically sees returns swing ±18% from its mean in any given year.",
        "red_flag": "Low recent volatility can mask tail risk; volatility clusters during crises.",
    },
    "Annual Volatility": {
        "definition": "Standard deviation of returns annualized to a 12-month scale.",
        "why_it_matters": "Standardized risk measure for comparing assets across timeframes.",
        "example": "A stock with 25% annual vol has wider year-to-year swings than the S&P 500's typical ~16%.",
        "red_flag": "Historic volatility understates risk during regime changes (rate shifts, crises).",
    },
    "Standard Deviation": {
        "definition": "A statistical measure of how spread out returns are around the mean.",
        "why_it_matters": "The foundational unit of risk for Sharpe, Sortino, and most risk models.",
        "example": "If average monthly return is 1% with std dev of 4%, most monthly returns land between -3% and +5%.",
        "red_flag": "Assumes normal distributions; real returns have fatter tails (black swan events).",
    },
    "Beta": {
        "definition": "A measure of how much an asset moves relative to the broader market.",
        "why_it_matters": "Tells you how much market risk (systematic risk) you're taking on.",
        "example": "Beta of 1.2 means when the S&P 500 rises 1%, your holding tends to rise 1.2%. Beta of 0.7 means it rises 0.7%.",
        "red_flag": "Beta is backward-looking and unstable — can shift sharply during regime changes.",
    },
    "Alpha": {
        "definition": "Risk-adjusted excess return above what the benchmark would predict for the risk taken.",
        "why_it_matters": "Shows if your manager/strategy is genuinely skilled or just riding beta.",
        "example": "If SPY returned 10% and your portfolio returned 12% at the same risk, your alpha is +2%.",
        "red_flag": "High alpha sounds great, but can be luck or hidden leverage. Look at 3+ year track records and t-stats.",
    },
    "Jensen's Alpha": {
        "definition": "Alpha calculated from the CAPM formula — return above what beta × market risk premium would predict.",
        "why_it_matters": "Isolates manager skill from market exposure.",
        "example": "Portfolio returned 14%, CAPM predicted 11% given its beta → Jensen's alpha = +3%.",
        "red_flag": "Only as good as your benchmark choice; wrong benchmark = misleading alpha.",
    },
    "R-squared": {
        "definition": "How much of a portfolio's movement is explained by its benchmark, from 0 to 1 (0% to 100%).",
        "why_it_matters": "High R² means alpha/beta numbers are meaningful; low R² means the benchmark is wrong.",
        "example": "An S&P 500 index fund will have R² near 1.0 vs SPY. A hedge fund might be 0.3.",
        "red_flag": "If R² < 0.7, beta-based metrics (alpha, Treynor) aren't reliable.",
    },
    "Information Ratio": {
        "definition": "Excess return over benchmark divided by the volatility of that excess return (tracking error).",
        "why_it_matters": "Measures how consistently a manager beats the benchmark, not just by how much.",
        "example": "Manager beats index by 2% annually with 4% tracking error → IR = 0.5. Above 0.5 is good, >1.0 exceptional.",
        "red_flag": "Small sample IRs can look amazing by chance; demand 5+ years of data.",
    },
    "Sortino Ratio": {
        "definition": "Like Sharpe, but only penalizes downside volatility — upside swings don't count as 'risk'.",
        "why_it_matters": "More intuitive for investors who care about losses, not gains.",
        "example": "If Sharpe is 0.8 but Sortino is 1.4, most of your 'volatility' is actually upside.",
        "red_flag": "Needs enough downside data; recent bull-only periods produce inflated Sortino numbers.",
    },
    "Treynor Ratio": {
        "definition": "Excess return per unit of market (systematic) risk — uses beta instead of total volatility.",
        "why_it_matters": "Useful when you hold a diversified portfolio and care only about market risk.",
        "example": "Return 10%, risk-free 4%, beta 0.9 → Treynor = (10-4)/0.9 = 6.67.",
        "red_flag": "Breaks down when beta is near zero or negative; only meaningful for well-diversified portfolios.",
    },
    "Tracking Error": {
        "definition": "Standard deviation of the difference between your portfolio's returns and its benchmark's returns.",
        "why_it_matters": "Tells you how closely a fund tracks (or strays from) its benchmark.",
        "example": "An index fund may have 0.1% tracking error. An active fund might have 4-6%.",
        "red_flag": "High tracking error in an index fund = poor execution; low tracking error in an active fund = closet indexing.",
    },
    "Tracking Difference": {
        "definition": "The realized gap between your portfolio's actual return and the benchmark's return over a period.",
        "why_it_matters": "This is what you actually earned vs what you expected — the real-world slippage.",
        "example": "SPY returned 20%, your S&P fund returned 19.7% → tracking difference is -0.3% (mostly expense ratio).",
        "red_flag": "Persistent negative tracking difference erodes compounding over decades.",
    },
    "Active Share": {
        "definition": "The percentage of a fund's holdings that differ from its benchmark (0% = identical to benchmark, 100% = no overlap).",
        "why_it_matters": "Identifies 'closet indexers' — active funds that charge high fees for index-like results.",
        "example": "A fund with 85% active share is genuinely active. One with 25% is a closet indexer.",
        "red_flag": "Active share below 60% combined with 1%+ expense ratio is a red flag — you're paying active fees for passive exposure.",
    },
    "Correlation": {
        "definition": "A number from -1 to +1 showing how closely two assets move together.",
        "why_it_matters": "Low or negative correlation between holdings is the engine of diversification.",
        "example": "Stocks and long-term Treasuries often have correlation near 0 or slightly negative — they hedge each other.",
        "red_flag": "Correlations jump toward +1 during crises — 'diversified' portfolios can all fall together.",
    },
    "Covariance": {
        "definition": "A measure of how two assets move together (correlation × both volatilities).",
        "why_it_matters": "The building block of portfolio-level volatility; shapes optimization results.",
        "example": "Two stocks with large positive covariance amplify each other's swings when combined.",
        "red_flag": "Like correlation, covariance is unstable in crisis — historical values mislead under stress.",
    },
    "Value at Risk": {
        "definition": "The worst expected loss over a period at a chosen confidence level.",
        "why_it_matters": "Simple, regulator-favored snapshot of tail risk.",
        "example": "1-day 95% VaR of -$50,000 means 95 out of 100 days you'll lose less than $50k (5 days could be much worse).",
        "red_flag": "VaR tells you nothing about what happens past the threshold — CVaR picks up the slack.",
    },
    "VaR": {
        "definition": "Value at Risk — the maximum loss expected with a given probability over a horizon.",
        "why_it_matters": "Industry-standard risk metric used for capital and limit setting.",
        "example": "5% 1-month VaR of -$100k means there's a 5% chance you lose more than $100k in a month.",
        "red_flag": "VaR breaks down in non-normal markets (crises), and it's silent on the severity of tail losses.",
    },
    "CVaR": {
        "definition": "Conditional VaR (also 'Expected Shortfall') — the average loss in scenarios worse than VaR.",
        "why_it_matters": "Captures tail severity that VaR ignores; preferred by Basel III and sophisticated investors.",
        "example": "95% VaR is -$50k but CVaR is -$90k → when you do lose more than $50k, you lose $90k on average.",
        "red_flag": "Still estimated from historical data — underestimates risk in regime shifts.",
    },
    "Expected Shortfall": {
        "definition": "Another name for CVaR — the average loss beyond the VaR threshold.",
        "why_it_matters": "Coherent risk measure that properly accounts for tail severity.",
        "example": "If 99% ES is -$200k, you'd expect $200k average loss in the worst 1% of outcomes.",
        "red_flag": "Requires enough extreme-tail data; often modeled rather than observed.",
    },
    "Max Drawdown": {
        "definition": "The largest peak-to-trough decline a portfolio has experienced over a period.",
        "why_it_matters": "Captures the pain you'd have had to live through — a better psychological risk gauge than volatility.",
        "example": "S&P 500 drawdown was -57% in 2008-09. Portfolios with -30% max drawdown recover much faster than -50%.",
        "red_flag": "Only shows the past; true maximum drawdown is always 'not yet realized'.",
    },
    "Herfindahl Index": {
        "definition": "A concentration metric: sum of squared weights in a portfolio. Ranges from 1/N (diversified) to 1.0 (single holding).",
        "why_it_matters": "Quantifies how concentrated your risk is in a small number of positions.",
        "example": "10 equal positions → HHI = 0.10. One position at 90% + rest tiny → HHI ≈ 0.81.",
        "red_flag": "HHI above 0.25 indicates heavy concentration — one bad name can tank the portfolio.",
    },
    "HHI": {
        "definition": "Herfindahl-Hirschman Index — sum of squared portfolio weights; higher = more concentrated.",
        "why_it_matters": "Simple, objective measure of concentration risk.",
        "example": "Equal 5-way split: HHI = 0.20. Two-stock portfolio: HHI = 0.50+.",
        "red_flag": "HHI > 0.18 is concentrated by antitrust standards — re-examine single-name risk.",
    },
    # ---------- Bonds / Fixed Income ----------
    "Yield to Maturity": {
        "definition": "The total annualized return you'd earn holding a bond until it matures, counting coupons and price pull-to-par.",
        "why_it_matters": "The bond equivalent of a CD's APY — how you compare bonds on apples-to-apples terms.",
        "example": "A 10-year bond bought at $95 with a 4% coupon may have a YTM around 4.6% (coupon + capital gain to $100 par).",
        "red_flag": "YTM assumes all coupons are reinvested at YTM — rarely true. Also ignores default risk.",
    },
    "YTM": {
        "definition": "Yield to Maturity — annualized total return if a bond is held to maturity.",
        "why_it_matters": "Standard metric for comparing bond investments.",
        "example": "A Treasury bought at par with a 4% coupon has a YTM of 4%.",
        "red_flag": "YTM assumes no default and no reinvestment drag.",
    },
    "Duration": {
        "definition": "A measure of a bond's price sensitivity to interest rate changes, expressed in years.",
        "why_it_matters": "Tells you how much your bond will drop if rates rise (and vice versa).",
        "example": "A bond with duration 7 will drop ~7% if rates rise 1%. A short-duration bond (duration 2) drops only ~2%.",
        "red_flag": "Long-duration bonds look safe until rates move — 2022 saw 20-year Treasuries drop ~30%.",
    },
    "Modified Duration": {
        "definition": "Duration adjusted to directly estimate % price change for a 1% rate move.",
        "why_it_matters": "Most practical form of duration — plug in a rate change, get a price estimate.",
        "example": "Modified duration 5, rates rise 50bp → price falls ~2.5%.",
        "red_flag": "Accurate only for small rate moves; convexity adjustment needed for larger shifts.",
    },
    "Effective Duration": {
        "definition": "Duration that accounts for embedded options (calls, puts, prepayments) — the 'real' rate sensitivity.",
        "why_it_matters": "More accurate than modified duration for mortgages, callable bonds, MBS.",
        "example": "A callable bond's effective duration shrinks when rates fall (because it's likely to be called).",
        "red_flag": "MBS effective duration shifts violently with rates — 'negative convexity' surprises retail investors.",
    },
    "Convexity": {
        "definition": "The curvature in the bond price–yield relationship — a second-order correction to duration.",
        "why_it_matters": "Positive convexity is good (more upside on rate drops than downside on rate rises); negative convexity is bad.",
        "example": "A 30-year zero-coupon bond has high positive convexity; MBS typically have negative convexity.",
        "red_flag": "Negative convexity in MBS means you lose more on rate rises than you gain on rate falls.",
    },
    "Coupon Rate": {
        "definition": "The fixed annual interest a bond pays, expressed as a percentage of par value.",
        "why_it_matters": "Determines your income stream from the bond, independent of price.",
        "example": "A $1,000 bond with a 4% coupon pays $40/year ($20 semi-annually) until maturity.",
        "red_flag": "Coupon rate ≠ yield — buying at a premium/discount changes your effective yield.",
    },
    "Par Value": {
        "definition": "The face value the bond issuer repays at maturity (usually $1,000 per bond).",
        "why_it_matters": "The anchor for coupon calculations and the amount you get back at maturity.",
        "example": "A bond at $950 priced 'at 95' trades 5% below par. At maturity, issuer still pays $1,000.",
        "red_flag": "Don't confuse par with market price — you could pay more than you get back.",
    },
    "Credit Spread": {
        "definition": "The extra yield a corporate or non-Treasury bond pays over a comparable-maturity Treasury — compensation for default risk.",
        "why_it_matters": "Widens in stress, tightens in calm — a barometer of credit market fear.",
        "example": "Corporate bond yields 6%, same-maturity Treasury yields 4% → credit spread = 2% (200 bp).",
        "red_flag": "Spreads widen fast in crises; if you need to sell, you sell low.",
    },
    "Investment Grade": {
        "definition": "Bonds rated BBB-/Baa3 or higher by credit agencies — low default risk, lower yields.",
        "why_it_matters": "Many institutional and retirement portfolios are restricted to IG-only bonds.",
        "example": "Apple bonds (AA+) are IG. Insurance companies and pensions buy them heavily.",
        "red_flag": "Ratings can be downgraded — 'fallen angels' (BBB → BB) cause forced selling waves.",
    },
    "High-Yield": {
        "definition": "Bonds rated below BBB-/Baa3 ('junk') — higher default risk, higher coupons.",
        "why_it_matters": "Offers equity-like returns with different risk drivers, but suffers in recessions.",
        "example": "A B-rated issuer might pay 8-10% yield vs 4-5% for IG, reflecting higher default odds.",
        "red_flag": "Junk spreads can blow out 500-800bp in recessions — correlates with equities in downturns.",
    },
    "Credit Quality": {
        "definition": "An overall rating of how likely a bond issuer is to repay (AAA = safest, D = in default).",
        "why_it_matters": "Determines expected return, volatility, and behavior in recessions.",
        "example": "A portfolio average credit quality of A means most bonds are solidly investment grade.",
        "red_flag": "Average credit quality can mask a few very risky holdings dragging the portfolio.",
    },
    "Bond Ladder": {
        "definition": "A strategy of buying bonds with staggered maturity dates so principal comes due each year.",
        "why_it_matters": "Produces a predictable cash flow schedule and reduces reinvestment risk.",
        "example": "Buy bonds maturing in 1, 2, 3, 4, 5 years — each year, one matures and you reinvest at current rates.",
        "red_flag": "A ladder locks in the current yield curve; steep inversions mean you lock in low long-end yields.",
    },
    "Maturity Bucket": {
        "definition": "A grouping of bonds by time-to-maturity (e.g., 1-3yr, 3-7yr, 7-10yr, 10yr+).",
        "why_it_matters": "Helps visualize concentration along the yield curve and manage duration risk.",
        "example": "A bond fund reports 30% in 1-3yr, 40% in 3-7yr, 30% in 7-10yr buckets.",
        "red_flag": "Heavy concentration in long-end buckets = high duration risk if rates rise.",
    },
    "Callable Bond": {
        "definition": "A bond where the issuer has the right to redeem (call back) the bond early, typically when rates fall.",
        "why_it_matters": "You're short a call option; if rates drop, you lose your high-coupon bond and must reinvest at low rates.",
        "example": "5% callable bond — if rates fall to 3%, issuer calls it and refinances cheaper. You reinvest at 3%.",
        "red_flag": "Call risk 'chops off' your upside — callable bonds behave differently in rally scenarios.",
    },
    "Floating-Rate Bond": {
        "definition": "A bond whose coupon adjusts periodically based on a reference rate (SOFR, T-Bills).",
        "why_it_matters": "Little price sensitivity to rising rates — good hedge against rate increases.",
        "example": "A floater paying SOFR + 1%: if SOFR rises from 3% to 5%, your coupon jumps from 4% to 6%.",
        "red_flag": "Credit risk is still there; also coupon falls when rates fall.",
    },
    "Zero-Coupon Bond": {
        "definition": "A bond that pays no periodic interest — sold at a deep discount, matures at par.",
        "why_it_matters": "Guarantees a known payoff at a specific date; high duration amplifies rate sensitivity.",
        "example": "10-year Treasury STRIP bought at $60 matures at $100 — ~5.2% annualized, no coupon.",
        "red_flag": "Very high duration — large price swings on rate moves. Also, phantom tax on accrued interest.",
    },
    # ---------- Retirement / 401(k) / IRA ----------
    "401(k)": {
        "definition": "An employer-sponsored, tax-deferred retirement account. Contributions reduce taxable income; withdrawals in retirement are taxed as ordinary income.",
        "why_it_matters": "The single most important wealth-building vehicle for most Americans, especially with employer match.",
        "example": "Contribute $23,500 (2025 limit) from salary; employer matches 50% up to 6% → up to 3% free.",
        "red_flag": "Early withdrawal before 59½ triggers 10% penalty plus ordinary income tax. Fund menu may be limited and expensive.",
    },
    "401k": {
        "definition": "Tax-deferred employer retirement plan — contributions come from pre-tax salary up to an annual limit.",
        "why_it_matters": "Tax deferral compounds for decades; employer match is immediate ~50-100% return.",
        "example": "Earning $100k, contributing 15% = $15k/year; employer adds a 5% match ($5k) = $20k/year.",
        "red_flag": "Not all 401(k)s are created equal — high expense ratios in some plan funds erode returns.",
    },
    "Traditional IRA": {
        "definition": "An individual retirement account where contributions may be tax-deductible, grows tax-deferred, taxed on withdrawal.",
        "why_it_matters": "For savers without a 401(k), or to supplement — more investment flexibility than a 401(k).",
        "example": "Contribute $7,000 (2025 limit, under 50) — deduct from income now, pay tax when withdrawn after 59½.",
        "red_flag": "Deductibility phases out at higher incomes if you have a workplace plan. RMDs start at age 73.",
    },
    "Roth IRA": {
        "definition": "An IRA funded with after-tax dollars; investments grow tax-free and qualified withdrawals are tax-free.",
        "why_it_matters": "Enormous long-term benefit for those who expect higher tax rates in retirement or want tax diversification.",
        "example": "Contribute $7,000 taxed today; 30 years later, all growth (potentially $50k+) is tax-free.",
        "red_flag": "Direct Roth contributions phase out at higher incomes; watch for the 5-year rule on earnings withdrawals.",
    },
    "SEP IRA": {
        "definition": "A Simplified Employee Pension IRA — a retirement plan for self-employed and small-business owners with high contribution limits.",
        "why_it_matters": "Lets self-employed shelter up to 25% of net self-employment income (capped at $70k in 2025).",
        "example": "A freelancer making $200k can contribute up to $50k to a SEP, all tax-deductible.",
        "red_flag": "If you have employees, SEP requires equal % contributions for them — can get expensive.",
    },
    "Solo 401(k)": {
        "definition": "A 401(k) for self-employed individuals with no employees (spouse OK); allows both employee and employer contributions.",
        "why_it_matters": "Higher combined contribution limits than SEP — employee + employer combo can exceed $70k+.",
        "example": "Sole proprietor makes $150k: $23,500 as employee + 25% employer = ~$51k total.",
        "red_flag": "More paperwork than SEP; must file Form 5500 once balance exceeds $250k.",
    },
    "Employer Match": {
        "definition": "The portion of your 401(k) contribution your employer adds on top — typically a percentage match up to a limit.",
        "why_it_matters": "This is free money — always contribute at least enough to get the full match.",
        "example": "Employer matches 100% of first 3% + 50% of next 2% → 4% free if you contribute 5%.",
        "red_flag": "Match may be subject to a vesting schedule — you could lose unvested match if you leave early.",
    },
    "Vesting Schedule": {
        "definition": "The schedule on which employer contributions become permanently yours; common schedules are 3-year cliff or 6-year graded.",
        "why_it_matters": "Leaving before vesting means leaving employer money on the table.",
        "example": "3-year cliff: 0% vested year 1-2, 100% at year 3. Leave at 2.5 years = lose all match.",
        "red_flag": "Know your vesting terms before planning a job change — could be tens of thousands of dollars.",
    },
    "Contribution Limits": {
        "definition": "IRS-set annual maximum you can contribute to retirement accounts.",
        "why_it_matters": "Determines how aggressively you can tax-shelter income each year.",
        "example": "2025 limits: 401(k) $23,500 + $7,500 catch-up if 50+; IRA $7,000 + $1,000 catch-up.",
        "red_flag": "Over-contributing triggers 6% excess tax per year until withdrawn.",
    },
    "Required Minimum Distribution": {
        "definition": "The mandatory annual withdrawal from traditional IRAs and 401(k)s starting at age 73 (74 for those born 1960+).",
        "why_it_matters": "Failure to take the RMD triggers a 25% penalty on what should have been withdrawn.",
        "example": "At 73 with $500k in a traditional IRA, first RMD is ~$18,868 (using life-expectancy factor).",
        "red_flag": "RMDs force taxable income in retirement — Roth conversions before 73 can soften the hit.",
    },
    "RMD": {
        "definition": "Required Minimum Distribution — forced annual withdrawal from tax-deferred retirement accounts starting at age 73.",
        "why_it_matters": "RMDs can push you into higher tax brackets; planning conversions earlier helps.",
        "example": "Large 401(k) at age 73 may force $30-50k+ withdrawals that bump you into a higher bracket.",
        "red_flag": "25% penalty on missed RMD (10% if fixed quickly); Roth IRAs have NO RMD for original owner.",
    },
    "Early Withdrawal Penalty": {
        "definition": "A 10% IRS penalty on withdrawals from traditional 401(k)/IRA before age 59½, on top of ordinary income tax.",
        "why_it_matters": "Turns what looks like a cash cushion into a heavily-taxed emergency fund.",
        "example": "Withdraw $10k at 45: $10k taxed as income + $1,000 penalty. You net ~$6-7k.",
        "red_flag": "72(t) SEPP and other exceptions exist, but they're narrow — consult before withdrawing.",
    },
    "Backdoor Roth": {
        "definition": "A strategy for high earners (above Roth income limits) to get money into a Roth IRA: contribute to a non-deductible traditional IRA, then convert to Roth.",
        "why_it_matters": "Opens Roth benefits to those otherwise phased out by income.",
        "example": "Earn $300k, over Roth limit. Contribute $7k non-deductible to tIRA, convert to Roth same year.",
        "red_flag": "'Pro-rata rule': if you have pre-tax traditional IRA money, conversion triggers taxes on a proportional basis.",
    },
    "Mega Backdoor Roth": {
        "definition": "A strategy in some 401(k) plans: make after-tax (non-Roth) contributions beyond the employee limit, then convert to Roth.",
        "why_it_matters": "Allows tens of thousands in extra Roth savings per year if your plan supports it.",
        "example": "Plan allows $46k after-tax contributions + $23,500 pre-tax → up to $69,500 total, much of it converted to Roth.",
        "red_flag": "Requires specific plan features (after-tax contribs + in-service conversion); most plans don't allow it.",
    },
    # ---------- Dividends / Income / Fees ----------
    "Dividend Yield": {
        "definition": "The annual dividend paid by a stock or fund divided by its current price, as a percentage.",
        "why_it_matters": "Shows income generation rate — important for retirees and income investors.",
        "example": "Stock at $100 paying $4/year in dividends has a 4% yield.",
        "red_flag": "High yields (>6-7%) often signal distress — the price may be falling faster than dividends.",
    },
    "Expense Ratio": {
        "definition": "The annual fee a mutual fund or ETF charges, expressed as a percentage of assets.",
        "why_it_matters": "Compounds drag on returns over decades; 1% fee = ~25% of final portfolio over 30 years.",
        "example": "SPY costs 0.09%; a typical actively-managed mutual fund might charge 0.75-1.25%.",
        "red_flag": "Anything above 0.50% needs a strong performance justification. Above 1% is a major drag.",
    },
    "Capital Gains Distribution": {
        "definition": "Gains realized inside a mutual fund (from its internal trading) passed through to shareholders, who owe tax on them.",
        "why_it_matters": "You can get a tax bill even on a fund you didn't sell — and even if the fund itself is down.",
        "example": "You hold XYZ fund at a 5% loss for the year; fund still distributes $2 per share in gains. You owe tax on that $2.",
        "red_flag": "Buying mutual funds near December distribution dates = immediate tax hit. ETFs largely avoid this.",
    },
    # ---------- Tax ----------
    "Wash Sale": {
        "definition": "An IRS rule (§1091) that disallows the loss on a sale if you buy the same (or 'substantially identical') security within 30 days before or after.",
        "why_it_matters": "Tax-loss harvesting fails if you trigger a wash sale — the loss is deferred, not denied.",
        "example": "Sell AAPL at a $5k loss on Dec 15, buy AAPL back Dec 20 → loss disallowed, added to new cost basis.",
        "red_flag": "Wash sale rule applies across ALL your accounts (IRA, spouse's account included) — often missed.",
    },
    "Tax-Loss Harvesting": {
        "definition": "Intentionally selling losing positions to offset realized gains (or up to $3,000/year of ordinary income).",
        "why_it_matters": "Can meaningfully reduce your tax bill each year — a key value-add in taxable accounts.",
        "example": "$10k realized gain + $7k realized loss = only $3k net taxable gain. You saved tax on $7k.",
        "red_flag": "Easy to accidentally trigger a wash sale when replacing with 'similar' funds (e.g., two S&P 500 ETFs).",
    },
    "Unrealized Gain/Loss": {
        "definition": "The paper gain or loss on a position you still hold — not yet taxable or deductible.",
        "why_it_matters": "Tells you what's sitting in the account ready to trigger tax consequences when you sell.",
        "example": "Bought AAPL at $100, now at $180 → $80/share unrealized gain. No tax until you sell.",
        "red_flag": "Unrealized losses reset your 'starting point' mentally, but don't help your tax bill until realized (and without a wash sale).",
    },
    # ---------- Other / Existing ----------
    "Allocation": {
        "definition": "How your portfolio is split across asset classes — stocks, bonds, cash, alternatives.",
        "why_it_matters": "Drives 80-90% of long-term portfolio risk and return — more important than stock-picking.",
        "example": "An 80/15/5 portfolio is aggressive (80% stocks); 40/50/10 is conservative.",
        "red_flag": "Drift happens — if stocks rally, your 60/40 may silently become 70/30. Rebalance periodically.",
    },
    "Concentration": {
        "definition": "The degree to which portfolio value is held in a small number of positions or sectors.",
        "why_it_matters": "High concentration = high idiosyncratic risk; one bad name or sector can sink performance.",
        "example": "If one stock is 40% of your portfolio, a 50% drop in it = 20% portfolio drop.",
        "red_flag": "Any single position >10% or sector >25% deserves a hard look.",
    },
    "Spread": {
        "definition": "The difference between two related prices or yields — most often, credit spread (corp vs Treasury) or bid-ask spread.",
        "why_it_matters": "A compact measure of risk (credit) or liquidity (bid-ask) that is often more informative than raw levels.",
        "example": "Corp bond yield 6%, Treasury 4% → 200bp credit spread. Bid $10.00 / Ask $10.05 → 5¢ bid-ask.",
        "red_flag": "Spread widening is often an early warning — credit distress or market stress.",
    },
    "Risk-Free Rate": {
        "definition": "The theoretical return on an investment with no risk of default — usually a short-term Treasury yield.",
        "why_it_matters": "The baseline against which all risky returns are compared (Sharpe, CAPM, etc.).",
        "example": "If 3-month T-Bill yields 5%, any risky strategy must exceed 5% to be worth the added risk.",
        "red_flag": "The 'risk-free' rate still has reinvestment risk and inflation risk — not truly risk-free.",
    },
}


def _term_to_text(term_name: str, entry) -> str:
    """Format a dictionary entry (structured or string) into a single explanation string."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        parts = []
        if entry.get("definition"):
            parts.append(entry["definition"])
        if entry.get("why_it_matters"):
            parts.append(f"Why it matters: {entry['why_it_matters']}")
        if entry.get("example"):
            parts.append(f"Example: {entry['example']}")
        if entry.get("red_flag"):
            parts.append(f"⚠️ {entry['red_flag']}")
        return " ".join(parts) if parts else f"See FINANCIAL_TERMINOLOGY.md for {term_name}"
    return f"See FINANCIAL_TERMINOLOGY.md for {term_name}"


def is_dr_stonk_enabled() -> bool:
    """Check if Dr. Stonk educational box is enabled.

    Returns False if INVESTORCLAW_DR_STONK_DISABLED env var is set to 'true'.
    """
    disabled = os.getenv("INVESTORCLAW_DR_STONK_DISABLED", "false").lower() == "true"
    return not disabled


def extract_terms_used(output_dict: Dict) -> Set[str]:
    """Scan output dict for financial terms that were used.

    Args:
        output_dict: Output dictionary from analysis pipeline

    Returns:
        Set of term names found in output
    """
    terms_found = set()

    # Flatten dict to string for searching
    flat_str = str(output_dict).lower()

    # Check for term keys (case-insensitive)
    for term in TERM_EXPLANATIONS.keys():
        if term.lower() in flat_str:
            terms_found.add(term)

    return terms_found


def build_dr_stonk_box(terms_used: Set[str], max_width: int = 80) -> str:
    """Build the Dr. Stonk educational footer box.

    Args:
        terms_used: Set of financial terms to explain
        max_width: Width of text box (for formatting)

    Returns:
        Formatted Dr. Stonk Box string
    """
    if not terms_used or not is_dr_stonk_enabled():
        return ""

    # Sort terms for consistent ordering
    sorted_terms = sorted(terms_used)

    lines = [
        "",
        "═" * max_width,
        "🖖 Dr. Stonk (From the planet Hephaestus) — Logical Explanations",
        "═" * max_width,
        "",
    ]

    for idx, term in enumerate(sorted_terms, 1):
        entry = TERM_EXPLANATIONS.get(term, f"See FINANCIAL_TERMINOLOGY.md for {term}")
        explanation = _term_to_text(term, entry)
        lines.append(f"[{idx}] {term}: {explanation}")
        lines.append("")

    lines.extend(
        [
            "👉 Comprehensive guide: https://gitlab.com/perlowja/InvestorClaw/-/blob/main/FINANCIAL_TERMINOLOGY.md",
            "🖖 Dr. Stonk says: Fascinating. Questions are logical, aren't they?",
            "💡 Disable explanations: INVESTORCLAW_DR_STONK_DISABLED=true or --no-dr-stonk CLI flag",
            "═" * max_width,
        ]
    )

    return "\n".join(lines)


def add_footnote_references(output_dict: Dict, terms_used: Set[str]) -> Dict:
    """Add footnote references [1], [2], etc. to output where terms appear.

    Args:
        output_dict: Output dictionary
        terms_used: Set of terms to add references for

    Returns:
        Modified output dict with footnote markers
    """
    if not terms_used or not is_dr_stonk_enabled():
        return output_dict

    # Create term-to-footnote mapping
    sorted_terms = sorted(terms_used)
    {term: f"[{idx}]" for idx, term in enumerate(sorted_terms, 1)}

    # Note: This is a simplified version. A full implementation would walk the dict
    # and add footnotes next to actual term occurrences. For now, we just return
    # the mapping so the renderer can use it.

    return output_dict
