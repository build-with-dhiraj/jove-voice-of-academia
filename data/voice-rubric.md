# Voice of Academia — Rubric (v1)

> **Co-designed by D Rage + orchestrator on 2026-05-23**, walking 15 Reddit threads (~150 comments) about JoVE. Locked at end of Phase 1.2. The LLM uses this file as the anchor for the binary YES/NO voice-candidate judgment downstream of theme tagging (see `pipeline/tag.py`).

---

## The judgment question (LOCKED)

> **"Would an experienced journalist quote this verbatim in an article about JoVE's reputation in academia? Yes / No."**

This is a single binary call per comment. It is **not** a sentiment judgment, **not** a theme judgment, and **not** a quality-of-the-author judgment. It is solely a quotability judgment.

The downstream consumer is D Rage's weekly Voice panel curation (D approves ~5–10 of the LLM's ~20–40 weekly YES candidates). Asymmetric trust: one sarcastic or empty comment surfaced in the Voice panel damages the whole panel's credibility. The LLM should be **conservative** — when in doubt, lean NO.

---

## YES criteria (general principles)

A comment qualifies for Voice-panel candidacy when it exhibits **one or more** of:

- **First-person experience** with specific detail (named editor, dollar amount, named institution, time duration, specific protocol, specific journal)
- **Concrete anecdote with a story arc** — beginning, middle, end. Something happened to this person and they're telling it.
- **Strong opinion backed by specific reasoning** — not just "JoVE sucks" but "JoVE charges $X for Y while delivering Z which I can demonstrate by …"
- **Multi-theme structured argument** — linking two or more themes into a coherent claim (e.g., fees → paywall → CV value)
- **Insider perspective** — someone speaking from a vantage point most readers wouldn't have (former employee, librarian, guest editor who actually did the role)
- **Named harm or named benefit** — concrete consequence, not vague vibes ("they spoofed an email from me to my librarian" vs "they're annoying")
- **Positive counter-examples** are equally welcome — a single concrete "this worked for me and here's why" is just as quotable as a negative anecdote

Length is not by itself a criterion. A 2-sentence concrete anecdote outranks a 5-paragraph generic rant.

---

## NO criteria (general principles)

A comment does **not** qualify when:

- **Generic agreement or one-word reactions** — "this", "agreed", "+1", "exactly", "Pass.", "Jove is bae"
- **Hearsay without first-person grounding** — "I have a friend who said JoVE is legit" with no specifics, or "I heard their fees are high"
- **Jokes or sarcasm without substantive content** — puns, memes, pop-culture quips that don't make a claim a journalist could quote
- **Pure questions** — "Anyone have experience with JoVE?" with no claim attached
- **Bare assertions** — "It's respected" / "Good for methods" / "Yes, they're legit" without any reasoning or example
- **Off-topic or tangential** to JoVE's reputation in academia (e.g., comment threads that drift to Roman mythology or unrelated journals without circling back)
- **Internally inconsistent or vague endorsement+caution** — "Yes legit, but watch out for fees" packs no quotable substance
- **Procedural or meta-thread comments** — "How do I delete this?" / "Mods please remove" / "Pasting in link is not working"

When a comment is borderline, the test is: **strip the quote of its context — does it still carry meaning a journalist could use?** If no, it's NO.

---

## YES exemplars

These 8 are drawn verbatim from real Reddit threads catalogued in `data/seed-threads.json`. They are the LLM's anchor for the YES side.

| # | Theme(s) | Quote | Source | Why YES |
|---|---|---|---|---|
| YES-1 | aggressive_marketing + legitimacy | "JoVE fascinates me in that they have historically published really useful material, but have dogshit editorial and marketing practices. I once filled out a survey for a JoVE marketer about how I could use JoVE in my teaching. They used my responses to spoof an email from me to my librarian requesting that we subscribe." | r/AskAcademia post 1nkbbm3, comment by deleted user | First-person + specific anecdote + named harm + journalistic-grade story arc |
| YES-2 | institutional_access + access_paywall | "Apparently they increased their subscription cost by 45% two years in a row, which is pretty insane. We actually straight up unsubscribed this year. It really is a shame, because their protocols tend to be fantastic." | r/labrats post 9hzpzh, comment by u/lit0st | Specific number + concrete consequence + mixed sentiment (price-negative, content-positive) |
| YES-3 | author_fees + access_paywall + author_cv_value | "They charge several thousand bucks to help produce your video, then it gets locked up behind a subscription payment wall. Meanwhile there is no original research finding being reported so the benefit to your CV is minimal." | r/AskAcademia post 1d9dnze, comment by u/parrotlunaire | Tight structured argument linking three themes (fees → paywall → CV) |
| YES-4 | legitimacy + peer_review_quality + peer_journal_comparison | "It was a different landscape when the journal started nearly 20 years ago. The idea of a video protocol is good considering how much info can be/is missing from published protocols. But the amount of overlap on basic/standard protocols is a cash cow. About 10 years ago it ramped up its output and it's all about profit." | r/AskAcademia post 1nkbbm3, comment by u/Fluffy-Antelope3395 | Historical context + insider critique + "cash cow" framing |
| YES-5 | author_cv_value + guest_editor_outreach (positive counter-example) | "A junior faculty in my lab became a guest editor and told me yeah it's a bit of a pain to direct a section, but hey I got my first last author paper like that and I think it's a great boost to both our careers so whatever." | r/labrats post 1s7vs2b, comment by u/HeyaGames | First-person + concrete career outcome + positive counter to dominant negative tone |
| YES-6 | video_format + reproducibility_value + methodology_focus (positive) | "For protocols requiring surgeries or otherwise complex procedures, having a video guide is absolutely invaluable. I used a Jove protocol to measure heart rate in Drosophila larvae, and I can count on one hand the number of other groups that examine Drosophila larvae heart rate." | r/labrats post 9hzpzh, comment by u/lit0st | Specific use case + reproducibility framing + niche-protocol concrete value |
| YES-7 | peer_review_quality + indexing_impact + author_fees (insider perspective) | "Their mission is to crank out as many videos as they can so they can collect publishing fees and increase their number of articles. They don't have an impact factor and really aren't very discriminating in what they accept, which I think has a dilutive effect." | r/AskAcademia post 29o6fu, comment by u/jovethrowaway (former JoVE employee) | Insider critique + multi-theme + structured argument |
| YES-8 | production_experience + video_format (positive mechanics) | "They have an actual team come into your lab and professionally record you over several days. For methods papers I think the video format is great. There is obviously also a paper that must be written." | r/AskAcademia post 95pq3y, comment by u/cag104 | Specific operational detail + first-person + positive |

---

## NO exemplars

These 8 illustrate the kinds of comments that should **not** clear the rubric, even when they touch real JoVE themes. They anchor the LLM's NO side.

| # | Quote | Source | Why NO |
|---|---|---|---|
| NO-1 | "Jove is bae" | r/labrats post 1s7vs2b, comment by u/Anal_Vengeance | Content-free fan reaction; no claim a journalist could quote |
| NO-2 | "Pass." | r/AskAcademia post 1d9dnze, comment by u/parrotlunaire | Pure dismissal. Note this user has a YES exemplar (YES-3) elsewhere — same user can produce both quotable and non-quotable comments. |
| NO-3 | "I have a friend who has 'published' with them, so they are a legit journal." | r/AskAcademia post 29o6fu, comment by u/Ijihata | Hearsay without first-person detail; explicitly rebutted in-thread |
| NO-4 | "Not gonna lie, I usually ignore those kinds of invites regardless of the journal." | r/AskAcademia post 1nkbbm3, comment by u/Few-Gur-3449 | Generic; not JoVE-specific. Strip context and it could be about any journal. |
| NO-5 | "JoVE is so predatory, Schwarzenegger is hunting them." | r/AskAcademia post 1d9dnze, comment by u/noknam | Joke without substantive content; a journalist can't quote this as commentary on JoVE's reputation |
| NO-6 | "Yes, they're legit. Look out for the fees they charge though." | r/AskAcademia post 29o6fu, comment by u/organiker | Too brief; vague endorsement+caution with no specifics |
| NO-7 | "Good for methods" | r/labrats post 1s7vs2b, comment by u/sgRNACas9 | Three-word claim with no reasoning |
| NO-8 | "It's respected." | r/labrats post 1s7vs2b, comment by u/onetwoskeedoo | Bare assertion; no reasoning, no example, no specific detail |

---

## How the LLM should use this file

1. **Read the judgment question first.** Every voice-candidate decision answers that one question.
2. **Apply the YES criteria as an OR-gate**: any one criterion satisfied is sufficient for YES, but stronger comments hit several.
3. **Apply the NO criteria as a screen**: if a comment looks like any NO exemplar above, it is NO regardless of theme relevance.
4. **When borderline, lean NO.** The downstream curator (D) re-reads every YES — surfacing 20 YES candidates is fine, surfacing a tepid one wastes curator attention.
5. **Return both the YES/NO verdict AND a one-sentence reasoning** so D can see why the LLM voted the way it did. The reasoning is the audit trail for rubric drift over time.
6. **Do not let theme strength override quotability.** A comment can correctly tag to `aggressive_marketing` and still be NO if it's a one-word reaction. Theme tagging and voice candidacy are independent judgments.
7. **First-person + specific detail is the strongest signal.** When in doubt, ask: "Did this person describe something that happened to them, with detail a journalist could fact-check?"

---

## Versioning

- **v1** (2026-05-23): initial co-design with D Rage. 8 YES + 8 NO exemplars from 15 validated Reddit threads.
- Subsequent revisions will be appended below with date, change summary, and the misjudgment that prompted the revision.
