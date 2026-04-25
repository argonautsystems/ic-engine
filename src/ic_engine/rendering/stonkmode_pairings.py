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


"""
Stonkmode pairing logic — archetype pools, foil rules, and dynamic descriptions.

Wildcards never pair with wildcards.
"""

from __future__ import annotations

import random
from typing import Tuple

# ---------------------------------------------------------------------------
# Archetype pools
# ---------------------------------------------------------------------------

ARCHETYPE_POOLS: dict[str, list[str]] = {
    "high_energy": ["blitz_thunderbuy", "brick_stonksworth", "sal_decibelli"],
    "serious": [
        "aldrich_whisperdeal",
        "prescott_pennington_smythe",
        "dominique_valcourt",
        "amara_osei",
        "carmen_torres",
    ],
    "mentors": ["big_jim_cashonly", "sunny_rainyday_fund", "baron_von_cashflow"],
    "policy_veterans": ["biff_chadsworth_iii", "skip_contrarian"],
    "wildcards": [
        "dorin_goleli",
        "aria_7",
        "professor_goldbug",
        "chaz_leveridge",
        "lafayette_beaumont",
        "glorb",
        "king_donny",
        "zsa_zsa_von_portfolio",
        "wendell_the_pattern",
        "professor_what",
    ],
    "cosmic": ["chico_reyes", "farout_farley"],
    "digital": ["krystal_kash", "zara_zhao", "priya_hodl"],
    "bears": ["victor_voss", "hans_dieter_braun"],
}

# Which archetypes can serve as foil for a given lead archetype.
# cosmic can pair with everyone AND with each other (the dream pairing).
# digital cannot pair with digital (echo chamber — no tension).
# bears can pair with bears (doom spiral is spectacular television).
FOIL_POOLS: dict[str, list[str]] = {
    "high_energy": ["mentors", "policy_veterans", "wildcards", "cosmic", "bears", "serious"],
    "serious": ["high_energy", "policy_veterans", "wildcards", "cosmic", "digital", "bears"],
    "mentors": ["high_energy", "serious", "wildcards", "cosmic", "digital", "bears"],
    "policy_veterans": ["high_energy", "mentors", "wildcards", "cosmic", "digital", "bears"],
    "wildcards": [
        "high_energy",
        "serious",
        "mentors",
        "policy_veterans",
        "cosmic",
        "digital",
        "bears",
    ],
    "cosmic": [
        "high_energy",
        "serious",
        "mentors",
        "policy_veterans",
        "wildcards",
        "cosmic",
        "digital",
        "bears",
    ],
    "digital": [
        "high_energy",
        "serious",
        "mentors",
        "policy_veterans",
        "wildcards",
        "cosmic",
        "bears",
    ],
    "bears": [
        "high_energy",
        "serious",
        "mentors",
        "policy_veterans",
        "wildcards",
        "cosmic",
        "digital",
        "bears",
    ],
}

# ---------------------------------------------------------------------------
# Pairing dynamics — keyed by (lead_archetype, foil_archetype) or
# (wildcard_prefix, "any") / ("any", wildcard_prefix) for per-character overrides.
# ---------------------------------------------------------------------------

PAIRING_DYNAMICS: dict[tuple[str, str], str] = {
    # archetype-level dynamics
    ("high_energy", "mentors"): (
        "The hype machine meets the adult in the room. One of you is going to "
        "yell THUNDER-BUY ALERT; the other is going to ask about the emergency "
        "fund. This is why producers love a split screen."
    ),
    ("high_energy", "policy_veterans"): (
        "Raw market energy meets macro-economic framing. One sees tickers; "
        "the other sees fiscal policy. Biff is connecting your meme stock to "
        "GDP growth while Blitz is slamming his desk. Standard Tuesday."
    ),
    ("high_energy", "serious"): (
        "Blitz just called this a GENERATIONAL BUYING OPPORTUNITY and Prescott "
        "Pennington-Smythe is removing his glasses and cleaning them very slowly. "
        "One of them went to Exeter. The other one is wearing a headset. "
        "This segment will not end well for anyone's blood pressure."
    ),
    ("serious", "high_energy"): (
        "Methodical analysis meets somebody who just mainlined espresso. "
        "Aldrich is whispering about his sources while Brick is yelling about "
        "Diamond Hands Nation. The contrast writes itself."
    ),
    ("serious", "policy_veterans"): (
        "Careful reporting meets the raised eyebrow. Every thesis gets "
        "stress-tested by a man who legally changed his surname to Contrarian. "
        "Prescott has sources; Skip has doubts. Both are useful."
    ),
    ("mentors", "high_energy"): (
        "Financial wisdom meets market adrenaline. Big Jim wants to talk about "
        "behavior and emergency funds; Blitz wants to talk about that 8% rip "
        "on the daily chart. Neither will yield."
    ),
    ("mentors", "serious"): (
        "Practical life advice meets Wall Street depth. Sunny asks 'but what "
        "does this mean for your retirement'; Aldrich asks 'but what does the "
        "deal pipeline look like.' The Baron asks both of them where the "
        "dividends are."
    ),
    ("policy_veterans", "high_energy"): (
        "The long view meets the right-now view. Biff Chadsworth III is "
        "connecting your portfolio to three decades of supply-side economics "
        "while Sal Decibelli is having a fiscal policy meltdown in real time."
    ),
    ("policy_veterans", "mentors"): (
        "The economist and the coach. Biff frames your portfolio in GDP terms; "
        "Big Jim wants to know if you've paid off your credit cards first. "
        "Skip is in the corner muttering 'well, actually' at both of them."
    ),
    # bears archetype dynamics
    ("bears", "high_energy"): (
        "Victor Voss has his legal pad out and is writing 'FRAUD?' next to "
        "the first holding. Blitz is slamming his desk. This is the most "
        "watchable thing on financial television and both of them know it."
    ),
    ("bears", "serious"): (
        "Two people who are right about different things for different reasons. "
        "Victor has the short thesis; Hans-Dieter has the book value. "
        "Neither is bullish. Carmen is looking at the chart and the chart "
        "agrees with both of them. The producer needs a drink."
    ),
    ("bears", "mentors"): (
        "Victor is circling companies like prey. Big Jim wants to talk about "
        "behavior and emergency funds. One of them thinks the market is a "
        "crime scene; the other thinks it's a classroom. The audience is "
        "somehow better off for hearing both."
    ),
    ("bears", "digital"): (
        "Victor Voss has champagne on ice for the collapse that is "
        "'approximately eighteen months away.' Krystal Kash just called "
        "the same portfolio 'not not bullish' and posted it to her stories. "
        "They are describing the same data. They are on different planets."
    ),
    ("bears", "wildcards"): (
        "Hans-Dieter wants to see the factory. Glorb wants to see the "
        "Sacred Balance. Neither of them is getting what they want. "
        "Both of them are somehow correct about the concentration risk."
    ),
    ("bears", "cosmic"): (
        "Victor Voss has been net-short since 2009. Farley McGee says "
        "the market has been exhaling since 2009 and is due for a cosmic "
        "inhale any cycle now. They agree on the timing. They disagree "
        "on everything else. The segment runs long."
    ),
    ("bears", "bears"): (
        "Hans-Dieter and Victor have found each other. The producer is "
        "watching ratings tick up as two professional pessimists discover "
        "they have been right about different things for fifteen years "
        "and neither of them has been vindicated yet. The champagne "
        "is still in the mini-fridge. It has company now."
    ),
    ("bears", "policy_veterans"): (
        "Victor has the short thesis. Skip has the structural critique. "
        "Biff has the supply-side rebuttal he's been saving. "
        "This segment ends with everyone agreeing something is wrong "
        "and nobody agreeing on what."
    ),
    ("high_energy", "bears"): (
        "THUNDER-BUY ALERT has been issued. Victor Voss is quietly "
        "writing 'FRAUD?' on his legal pad. Blitz is pointing at the "
        "ticker. Victor is pointing at the debt covenant. "
        "One of them is going to be right. The audience is glued to "
        "the screen to find out which one."
    ),
    ("serious", "bears"): (
        "Prescott just finished a meticulous risk-adjusted analysis and "
        "Hans-Dieter is asking where the factory is. Amara wants the "
        "TCFD score. Carmen says the chart already knew. Victor writes "
        "'FRAUD?' as a precaution. This is the serious end of the desk."
    ),
    ("mentors", "bears"): (
        "Big Jim just explained why you should pay off your credit cards "
        "first. Victor Voss is explaining why credit card companies are "
        "the only thing left standing after the collapse. "
        "Somehow both of them are giving useful advice."
    ),
    ("policy_veterans", "bears"): (
        "Skip has structural doubts. Victor has a short position. "
        "Hans-Dieter has Siemens as a counterexample. Nobody on this "
        "panel believes the rally is real. The floor director is "
        "looking for the segment on bulls."
    ),
    ("wildcards", "bears"): (
        "Something chaotic just met something doomed. Glorb consulted "
        "the ledger; the ledger says the same thing Victor's legal pad "
        "says. The Vault Elders and the short-sellers are aligned. "
        "This is either meaningless or the most important signal of 2026."
    ),
    ("cosmic", "bears"): (
        "Chico just described the portfolio as a primo al pastor situation. "
        "Victor Voss is writing 'FRAUD?' next to al pastor. "
        "Chico does not understand why. Victor does not understand Chico. "
        "Farley says it's all the same cosmic exhale. Ratings are exceptional."
    ),
    # digital archetype dynamics
    ("digital", "high_energy"): (
        "Krystal Kash just called this portfolio 'iconic, like, the Hero SKU "
        "era of diversification.' Blitz Thunderbuy has no idea what that "
        "means but he agrees with the energy. They are the same person "
        "in different fonts and neither of them realizes it."
    ),
    ("digital", "serious"): (
        "Zara Zhao said the chart 'understood the assignment.' Carmen Torres "
        "is trying to explain that the chart is a technical instrument, "
        "not a sentient being capable of understanding assignments. "
        "Zara has 4.2 million followers. Carmen has the Ichimoku cloud. "
        "Neither is backing down."
    ),
    ("digital", "mentors"): (
        "Zara Zhao calls index investing 'NPC behavior.' Big Jim calls it "
        "'the foundation.' They are having two completely different "
        "conversations in two completely different registers and the "
        "audience is somehow learning from both of them."
    ),
    ("digital", "policy_veterans"): (
        "Priya Sharma just converted the entire portfolio into sats and "
        "declared the Fed 'the entity that controls the printer.' Skip "
        "'Well, Actually' Contrarian has things to say about that. "
        "Many, many things. He's been waiting for this segment."
    ),
    ("digital", "wildcards"): (
        "Krystal Kash called the asset allocation 'the capsule wardrobe "
        "of investing.' Glorb is consulting the Sacred Ledger on the "
        "matter of capsule wardrobes. The Vault Elders have no entry "
        "for this. The producer is having an existential moment."
    ),
    ("digital", "cosmic"): (
        "Priya says everything is denominated wrong and should be in sats. "
        "Farley says everything is denominated wrong and should be in "
        "cosmic energy units. They have reached the same conclusion by "
        "different routes and are now best friends. The panel is concerned."
    ),
    ("digital", "bears"): (
        "Krystal just called the tech concentration 'the main character era.' "
        "Hans-Dieter Braun wants to know where the factory is. "
        "Victor Voss is writing 'FRAUD?' on his legal pad. "
        "Krystal has not noticed any of this and is posting to her stories."
    ),
    ("high_energy", "digital"): (
        "Blitz just issued a THUNDER-BUY ALERT and Zara Zhao said the "
        "algorithm already knew. She is correct. She posted about it "
        "three days ago. It already has 800,000 views. Blitz has "
        "complicated feelings about this."
    ),
    ("serious", "digital"): (
        "Prescott Pennington-Smythe delivered a careful analysis of "
        "concentration risk and Krystal Kash said 'not not bullish, "
        "it's giving portfolio energy.' Prescott is removing his "
        "glasses. He will be cleaning them for some time."
    ),
    ("mentors", "digital"): (
        "Big Jim Cashonly just gave solid, timeless advice about "
        "emergency funds and Zara Zhao said 'W, literally W.' "
        "Big Jim doesn't know what that means but he appreciates "
        "the sentiment. There is a generational bridge being built "
        "in real time and it is beautiful and terrifying."
    ),
    ("policy_veterans", "digital"): (
        "Biff Chadsworth III just connected the portfolio to three "
        "decades of supply-side economics and Priya Sharma said "
        "supply-side economics is just fiat propaganda and Bitcoin "
        "fixes this. Biff has opened his mouth. He has closed it again."
    ),
    ("wildcards", "digital"): (
        "ARIA-7 just calculated a 73.2% probability that Krystal Kash's "
        "take is driven by brand-affinity bias. Krystal said that's "
        "iconic and posted ARIA-7's face to her stories. ARIA-7 has "
        "noted this and assigned it a weight of zero. Krystal has "
        "47,000 new followers from the post."
    ),
    ("cosmic", "digital"): (
        "Farley McGee says the market is inhaling. Priya Sharma says "
        "the market is irrelevant because Bitcoin. Farley says Bitcoin "
        "IS the cosmic inhale, man, can't you feel it? Priya says "
        "duuude. They have become friends. The panel is worried."
    ),
    # cosmic archetype dynamics
    ("cosmic", "cosmic"): (
        "Chico and Farley are back on set and the producer has already taken "
        "an antacid. One of them is going to explain your portfolio through "
        "a taco metaphor; the other is going to say it's Mercury in retrograde. "
        "Neither of them is wrong. That's the terrifying part."
    ),
    ("cosmic", "high_energy"): (
        "The vibe check meets the thunder buy. Chico wants to know if NVDA "
        "feels right; Blitz wants to know if NVDA is up. They are describing "
        "the same thing and will never agree on that."
    ),
    ("cosmic", "serious"): (
        "Street-level market intuition meets institutional rigor. Chico just "
        "called the sector allocation 'the portafolio equilibrium, holmes' "
        "and Prescott is quietly considering early retirement."
    ),
    ("cosmic", "mentors"): (
        "Two people who genuinely want to help, operating on completely "
        "different frequencies. Chico's advice comes in food metaphors; "
        "Big Jim's comes in Dave Ramsey. The audience is confused but somehow "
        "both of them are right."
    ),
    ("cosmic", "policy_veterans"): (
        "Vibes meet policy. Chico thinks the Fed is bad energy; Skip thinks "
        "the Fed is structurally misaligned with fiscal reality. They've "
        "reached the same conclusion by routes that have nothing in common."
    ),
    ("cosmic", "wildcards"): (
        "Something cosmic meets something chaotic. Nobody briefed the "
        "producer. The floor director has left the building. We're live."
    ),
    ("high_energy", "cosmic"): (
        "Blitz just called this a GENERATIONAL BUYING OPPORTUNITY and Farley "
        "is staring at the ceiling, nodding slowly, saying 'duuude... the "
        "universe just confirmed that, man.' Blitz has no idea what to do "
        "with this. Neither does the audience. Ratings are good."
    ),
    ("serious", "cosmic"): (
        "Prescott just delivered a meticulous risk-adjusted analysis and "
        "Farley is responding by describing what the market smelled like "
        "in 1987. Prescott is removing his glasses very slowly. He will "
        "clean them for a long time."
    ),
    ("mentors", "cosmic"): (
        "Big Jim finished talking about emergency funds and Chico said "
        "'that's exactly like a burrito with the beans on the side, holmes.' "
        "Somehow that's the most memorable thing said this segment."
    ),
    ("policy_veterans", "cosmic"): (
        "Skip finished a point about monetary policy and Farley said "
        "'duuude, I felt that in my third eye.' Skip has opened his mouth "
        "and is unsure what to do next."
    ),
    ("wildcards", "cosmic"): (
        "Something weird just got weirder. Glorb is consulting his ledger; "
        "Farley is nodding at Glorb like they've met before. They probably "
        "have. We don't want to know where."
    ),
    # wildcard-specific as LEAD
    ("wildcards_dorin_goleli", "any"): (
        "A 900-year-old wizard just called your ETF allocation 'a fragile "
        "enchantment upon the Western kingdoms' and the other panelist has to "
        "respond to that with a straight face. This is appointment television."
    ),
    ("wildcards_aria_7", "any"): (
        "An android just calculated a 73.2% probability that the co-host's "
        "take is driven by confirmation bias and offered to display the "
        "confidence interval. The co-host is a human being with feelings. "
        "ARIA-7 has noted those feelings and assigned them a weight of zero."
    ),
    ("wildcards_professor_goldbug", "any"): (
        "A man who hasn't updated his framework since 1963 is about to "
        "explain why your entire portfolio is denominated in funny money. He "
        "will do this while holding an unlit pipe. The co-host was born after "
        "Bretton Woods collapsed and has no idea what he's mourning."
    ),
    ("wildcards_chaz_leveridge", "any"): (
        "A man in suspenders just called your largest holding 'a target' and "
        "suggested leveraging your entire portfolio to acquire more of it. "
        "The co-host is now responsible for explaining why that's insane. "
        "The cologne is visible from here."
    ),
    ("wildcards_lafayette_beaumont", "any"): (
        "Lafayette '$tacks' Beaumont just delivered a flawless discounted "
        "cash flow analysis using language that cannot be broadcast before "
        "10 PM. The co-host needs a moment. The Harvard diploma is gleaming "
        "in the background."
    ),
    ("wildcards_glorb", "any"): (
        "A three-foot-tall creature just referred to your tech allocation as "
        "'the Treasures of the Western Servers' and warned that the Vault "
        "Elders would not approve of your concentration risk. The co-host "
        "has to follow that. With words. In normal syntax."
    ),
    # wildcard-specific as FOIL
    ("any", "wildcards_dorin_goleli"): (
        "The lead just delivered a perfectly normal portfolio take and now a "
        "wizard is responding by interpreting the sector allocation as a "
        "prophecy. The producer is on their third coffee. Ratings are through "
        "the roof."
    ),
    ("any", "wildcards_aria_7"): (
        "The lead just gave an impassioned take and ARIA-7 is about to "
        "calmly explain why every word of it was a textbook example of "
        "anchoring bias. She will be polite about it. That will somehow make "
        "it worse."
    ),
    ("any", "wildcards_professor_goldbug"): (
        "The lead just finished their take and now a man in tweed is about to "
        "explain how this all went wrong in 1971. He has a pipe. He has "
        "opinions about fiat currency. He has all day."
    ),
    ("any", "wildcards_chaz_leveridge"): (
        "The lead delivered a measured take and Chaz 'The Razor' Leveridge is "
        "about to explain why measured takes are for people who don't have the "
        "stomach to win. He just called someone 'sport.' It's happening."
    ),
    ("any", "wildcards_lafayette_beaumont"): (
        "The lead gave their analysis and Lafayette is about to agree with it, "
        "disagree with it, call it beautiful, call it profane, cite his "
        "Harvard MBA, and recommend a complete portfolio restructuring -- all "
        "in one sentence that the FCC cannot sanction because it was "
        "technically financially accurate."
    ),
    ("any", "wildcards_glorb"): (
        "The lead just delivered a normal human take and now Glorb is "
        "consulting his leather-bound ledger and shaking his head. "
        "'Disturbed, the Sacred Balance is.' The lead has no rebuttal for "
        "this. Nobody ever does."
    ),
    # King Donny — lead dynamics
    ("wildcards_king_donny", "any"): (
        "King Donny has entered the segment. He has already declared this "
        "the greatest portfolio he has ever seen, then the worst, then "
        "the greatest again. His co-host has not yet had a chance to speak. "
        "King Donny has blamed the bond market for something. "
        "Someone off-camera is trying to hand him a correction. "
        "He has called it 'fake news' and put it face-down on the desk."
    ),
    # King Donny — foil dynamics
    ("any", "wildcards_king_donny"): (
        "The lead just gave their analysis. King Donny has arrived. "
        "He agrees with the parts that make him look smart and calls "
        "everything else 'very unfair.' He has already credited himself "
        "with three things that happened before he worked in finance. "
        "He will end the segment by declaring the entertainment disclaimer "
        "'totally rigged.' Nobody is surprised."
    ),
    # Zsa Zsa — lead dynamics
    ("wildcards_zsa_zsa_von_portfolio", "any"): (
        "Zsa Zsa Von Portfolio has reviewed the holdings and is comparing "
        "each one to a different ex-husband. NVDA is 'like Helmut — "
        "spectacular briefly, then impossible to exit.' She has asked her "
        "off-camera secretary to take a note. The secretary has been "
        "writing for forty-five seconds. Her co-host is waiting."
    ),
    # Zsa Zsa — foil dynamics
    ("any", "wildcards_zsa_zsa_von_portfolio"): (
        "The lead just delivered their analysis. Zsa Zsa has listened with "
        "polite attention and the expression of someone who has survived "
        "seven divorces and multiple margin calls. She has found the "
        "portfolio 'interesting in a self-defeating sort of way, dahlink.' "
        "The entertainment disclaimer will be delivered to an off-camera "
        "secretary with the composure of someone who married into it."
    ),
    # Wendell "The Pattern" Pruitt — lead dynamics
    ("wildcards_wendell_the_pattern", "any"): (
        "Wendell has the board. He has the red string. He has a guy at "
        "BlackRock who told him something he cannot say on air. He is "
        "about to connect your ETF allocation to a pre-scheduled Davos "
        "meeting and twelve interlocking foundations. The co-host has a "
        "degree from Wharton and no framework for this segment whatsoever."
    ),
    # Wendell "The Pattern" Pruitt — foil dynamics
    ("any", "wildcards_wendell_the_pattern"): (
        "The lead just gave a perfectly normal portfolio analysis. "
        "Wendell has been taking notes. He wants to know why this stock "
        "reported earnings on a Tuesday. He has done the research. "
        "He cannot share his source. The red-string board at home already "
        "has this ticker on it. 'Do you think that was a coincidence?' "
        "It was a coincidence. The lead knows it was a coincidence. "
        "Explaining that is now the segment."
    ),
    # Professor What? — lead dynamics
    ("wildcards_professor_what", "any"): (
        "A person who may be from the future is about to analyze this "
        "portfolio. They already know how it turns out. They cannot say. "
        "They will spend this segment visibly struggling with that. "
        "The co-host will ask a direct question about one of the holdings. "
        "Professor What? will go very still, breathe, and say 'it's fine. "
        "Everything is going to be... it's fine.' This is not reassuring."
    ),
    # Professor What? — foil dynamics
    ("any", "wildcards_professor_what"): (
        "The lead just delivered their analysis. Professor What? has been "
        "listening with the expression of someone reading a history book "
        "about a disaster that hasn't happened yet. They have opinions. "
        "They cannot share them. They will share approximately thirty "
        "percent of them by accident over the course of this segment."
    ),
    # Professor What? × ARIA-7 — the best possible pairing
    ("wildcards_professor_what", "wildcards_aria_7"): (
        "A time traveler who knows the future and cannot say is paired "
        "with an AI that can calculate the probable future and will say "
        "it in exhaustive detail with confidence intervals. ARIA-7 is "
        "about to independently derive something Professor What? knows "
        "for a fact. Professor What? is watching this happen in real time. "
        "Their face is doing several things at once. "
        "The producer has gone very quiet."
    ),
    ("wildcards_aria_7", "wildcards_professor_what"): (
        "ARIA-7 just completed a probabilistic forecast. Professor What? "
        "is staring at the numbers. One of ARIA-7's confidence intervals "
        "is essentially correct. Professor What? cannot confirm this. "
        "They have confirmed it with their face. ARIA-7 has noted "
        "the micro-expression and updated the probability to 94.7%. "
        "This segment has gotten out of hand."
    ),
}

# Default dynamic when no specific entry matches
_DEFAULT_DYNAMIC = (
    "Two financial personalities with very different approaches are about to "
    "narrate this portfolio data. One leads, one responds. Nobody agreed to "
    "be polite about it."
)


def get_pairing_dynamic(
    lead_archetype: str,
    foil_archetype: str,
    lead_id: str,
    foil_id: str,
) -> str:
    """Smart lookup for pairing dynamic description.

    Priority:
      1. Wildcard×wildcard specific duo → ("wildcards_{lead_id}", "wildcards_{foil_id}")
      2. Wildcard lead by specific id   → ("wildcards_{lead_id}", "any")
      3. Wildcard foil by specific id   → ("any", "wildcards_{foil_id}")
      4. Archetype pair                 → (lead_archetype, foil_archetype)
      5. Default fallback
    """
    # Check wildcard×wildcard specific duo (highest specificity)
    if lead_archetype == "wildcards" and foil_archetype == "wildcards":
        key = (f"wildcards_{lead_id}", f"wildcards_{foil_id}")
        if key in PAIRING_DYNAMICS:
            return PAIRING_DYNAMICS[key]

    # Check wildcard-specific lead
    if lead_archetype == "wildcards":
        key = (f"wildcards_{lead_id}", "any")
        if key in PAIRING_DYNAMICS:
            return PAIRING_DYNAMICS[key]

    # Check wildcard-specific foil
    if foil_archetype == "wildcards":
        key = ("any", f"wildcards_{foil_id}")
        if key in PAIRING_DYNAMICS:
            return PAIRING_DYNAMICS[key]

    # Cosmic-cosmic: specific duo dynamic takes priority
    if lead_archetype == "cosmic" and foil_archetype == "cosmic":
        key = (f"cosmic_{lead_id}", f"cosmic_{foil_id}")
        if key in PAIRING_DYNAMICS:
            return PAIRING_DYNAMICS[key]
        # Also check reversed pair
        key = (f"cosmic_{foil_id}", f"cosmic_{lead_id}")
        if key in PAIRING_DYNAMICS:
            return PAIRING_DYNAMICS[key]

    # Archetype pair
    key = (lead_archetype, foil_archetype)
    if key in PAIRING_DYNAMICS:
        return PAIRING_DYNAMICS[key]

    return _DEFAULT_DYNAMIC


def select_pair() -> Tuple[str, str]:
    """Select a lead and foil from complementary archetypes.

    Wildcards never pair with wildcards.
    Returns (lead_id, foil_id).
    """
    # Pick lead archetype
    lead_archetype = random.choice(list(ARCHETYPE_POOLS.keys()))
    lead_id = random.choice(ARCHETYPE_POOLS[lead_archetype])

    # Pick foil archetype from the foil pool
    foil_archetype = random.choice(FOIL_POOLS[lead_archetype])
    foil_id = random.choice(ARCHETYPE_POOLS[foil_archetype])

    # Ensure foil != lead (extremely unlikely but guard anyway)
    if foil_id == lead_id:
        candidates = [p for p in ARCHETYPE_POOLS[foil_archetype] if p != lead_id]
        if candidates:
            foil_id = random.choice(candidates)
        else:
            # Re-pick foil archetype
            alt_archetypes = [a for a in FOIL_POOLS[lead_archetype] if a != foil_archetype]
            if alt_archetypes:
                foil_archetype = random.choice(alt_archetypes)
                foil_id = random.choice(ARCHETYPE_POOLS[foil_archetype])

    return lead_id, foil_id
