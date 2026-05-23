# Taxonomy Assignment Exemplars

> **Co-designed by D Rage + orchestrator on 2026-05-23**, derived from 15 validated Reddit threads (~150 comments) about JoVE. This file is the LLM's anchor for the **theme-tagging** step of `pipeline/tag.py` — it shows, per canonical theme, what real Reddit comments tagged to that theme actually look like.

## How the LLM should use this file

1. For each comment, after sentiment classification, decide which of the 16 canonical themes apply. **A single comment can apply to multiple themes** (per D1 in the system design — multi-row generation is intended).
2. Use these exemplars to anchor on the **shape and substance** of a real tag, not just the keyword. The example phrasings in `taxonomy.json` give the surface; this file gives the depth.
3. Themes are **mutually exclusive within a sentiment bucket** per D1. A comment that is `negative` about `author_fees` and `negative` about `legitimacy` produces two rows (one per theme); a comment is **not** tagged twice to the same theme with two sentiments.
4. When you cannot confidently fit a comment to one of the 16, route it to `emerging_other` and provide a one-sentence summary of why none fit.

## Provenance notice — REAL vs FABRICATED entries

- **`[REAL]`** prefixed exemplars are verbatim quotes from validated Reddit threads listed in `data/seed-threads.json`. Source attribution: `(r/<subreddit> <post_id>, <commenter handle>)`.
- **`[FABRICATED — reference example]`** prefixed exemplars are authored by D Rage + orchestrator during the 2026-05-23 co-design session, anchored on the theme's `description` and on patterns observed in the real data. They extend coverage for thin-data themes; they are **never** to be confused with real Reddit content.
- **Never** treat a `[FABRICATED]` entry as if it were real Reddit data downstream (e.g., do not surface it in the Voice panel, do not count it in frequency aggregates). Its job is **anchoring the LLM's tag judgment** at training-prompt time only.

---

## 1. access_paywall — Reader access / paywall friction

> Reader-side friction accessing JoVE videos or protocols. Paywall complaints, sci-hub attempts, requests for access help from peers, university VPN workarounds, frustration that videos require subscription.

### Exemplars

- `[REAL]` "Nah, Jove isn't bundled, and their subscription costs are so expensive our library asked us if anyone was using them or if they could unsubscribe." — (r/labrats 9hzpzh, u/lit0st)
- `[REAL]` "Most of the time being on a university WiFi network gives you access to whatever they have access to. That or if you have a friend at a different university you could use their login to vpn into their network" — (r/labrats q3nr48, u/Apprehensive-Pop3823)
- `[REAL]` "Pasting in link is not working" → "I see. Don't think sci-hub is your way forward then." — (r/scihub ksx87q exchange)

---

## 2. guest_editor_outreach — Guest editor / special issue invitations

> JoVE inviting someone to be a guest editor of a special issue. Includes complaints about spam-feel, fishiness, lack of editorial autonomy.

### Exemplars

- `[REAL]` "JoVE is a legitimate methods journal. They do have an aggressive solicitation campaign to get people to submit and be guest editors. Guest editor means you would have to help them find articles for the topic, presumably from your friends and colleagues." — (r/AskAcademia hjg41o, u/DoctorPhD)
- `[REAL]` "I have been involved as editor to another special issues of other journals. I still had full control of who I invite and what paper I accept" — (r/AskAcademia 1nkbbm3, OP u/anakreontas — complaint about lack of editorial autonomy)
- `[REAL]` "Wouldn't consider it. These journals spam these invites." — (r/AskAcademia 1d9dnze, u/RadDadJr)

---

## 3. author_outreach — Author solicitation / article-conversion invitations

> JoVE inviting someone to turn an existing paper into a JoVE video, or to submit a new manuscript directly. Distinct from `guest_editor_outreach`: this is about your own paper as author, not editing others' work.

### Exemplars

- `[REAL]` "They have asked me to publish a bunch of times and it made absolutely no sense to me because there was nothing about the work that would benefit from being presented in video format. It came off as predatory to me and I have turned them down each time." — (r/AskAcademia 95pq3y, u/zinfandelightful)
- `[REAL]` "They are gaining a reputation as a 'low tier journal' in part due to their solicitation of articles based off of published papers." — (r/AskAcademia 95pq3y, u/DoctorPhD)
- `[REAL]` "Also got similar e-mails from them, mdpi and frontiers. They aren't predatory publishers and have legitimate review processes, but they are aggressive in recruiting people that will make other people submit papers." — (r/AskAcademia hjg41o, u/3d_extra)

---

## 4. legitimacy — Legitimacy / predatory feel

> Whether JoVE is a legitimate scientific journal vs a predatory operation. Includes vanity-publishing concerns, MDPI-comparison, the "gray area" framing.

### Exemplars

- `[REAL]` "It's not predatory and it has a fairly good reputation. Downsides are low impact factor and high publication costs due to the entire filming process." — (r/labrats 1s7vs2b, u/Twintig-twintig)
- `[REAL]` "I'm baffled at people saying it's not predatory with how many spam invitations I get to submit topics that are not my expertise. They're as bad as MDPI as far as I've seen." — (r/labrats 1s7vs2b, u/Sophsky)
- `[REAL]` "My read is from a quick Google search is that they do some innovative things and also use some predatory practices, so rather than think of it as predatory or not in a black or white sense, it seems to be in somewhat of a gray area." — (r/AskAcademia hjg41o, u/jogam)
- `[REAL]` "Not predatory and actually one of the most unique journals." — (r/labrats 1s7vs2b, u/LeJeansGenes)

---

## 5. reputation_with_pis — Reputation among senior researchers and PIs

> How senior researchers, advisors, and PIs view JoVE; their guidance to junior researchers about whether to publish there; the "it's not Nature" framing.

### Exemplars

- `[REAL]` "I was offered that as a late stage graduate student and it didn't seem worth the significant effort unless you had your own method to contribute." — (r/labrats 1s7vs2b, u/BoltVnderhuge)
- `[REAL]` "Boss was a bit mimimi because it is not NATURE. But hey, paper has been cited 40x so far. So quite ok for something that probably helped quite a few people out." — (r/labrats 1s7vs2b, u/indiode)
- `[FABRICATED — reference example]` "My advisor actively encouraged me to submit our microfluidic device fabrication protocol to JoVE — said the visual demonstration would do more for our lab's methods reputation than another middle-author paper in a niche journal."

---

## 6. video_format — Video-as-format value, suitability, watchability

> Discussion of the video medium itself — whether video helps for protocols, format suitability for one's research type, watchability of seeing oneself on video, format-vs-text trade-off.

### Exemplars

- `[REAL]` "I don't know about prestige, but I love JoVE. It's helped me refine some of my own techniques, and I even like watching videos on stuff that I don't work on." — (r/labrats 1s7vs2b, u/tendonsofsteel)
- `[REAL]` "I've published there. They are fine. It's a bit of a hassle and cost, due to their format, but it wasn't overwhelming. Worst part is seeing/hearing myself on video." — (r/labrats 1s7vs2b, u/orthomonas)
- `[REAL]` "Others in my department (zebrafish people) love it since it shows a real in-depth look at protocols as they are being performed in front of you, which is absolutely valuable to anyone." — (r/biology mtfkd, deleted user)
- `[REAL]` "Make sure that your technique lends itself better to video than paper." — (r/AskAcademia 29o6fu, u/jovethrowaway)

---

## 7. author_fees — Author-side publication fees

> The cost JoVE charges authors to publish: ~$2,400 video production fee + $1,800 OA add-on. Includes comparison to other journals' fees.

### Exemplars

- `[REAL]` "Downsides are low impact factor and high publication costs due to the entire filming process. They also have very specific demands on how the protocol should be written and on the filming script." — (r/labrats 1s7vs2b, u/Twintig-twintig)
- `[REAL]` "There is a fee to publish with them but everything is peer reviewed." — (r/AskAcademia hjg41o, u/DoctorPhD)
- `[REAL]` "The publication fee is a bit higher than other journals, but you are paying for a videographer and such with those costs." — (r/AskAcademia 95pq3y, u/DoctorPhD)

---

## 8. peer_review_quality — Peer-review rigor and acceptance bar

> Quality and rigor of JoVE's peer review process, reviewer depth/engagement, perceived selectivity, rubber-stamp suspicions. ABSORBS the former `acceptance_bar` theme — Reddit voices conflate these in practice.

### Exemplars

- `[REAL]` "Reviewers watch to make sure it doesn't cut to weird stuff halfway through, and bam, a new line on your CV for free." — (r/labrats 9hzpzh, deleted user — sarcastic rubber-stamp critique)
- `[REAL]` "Thousands of dollars go into our video productions, and all of our articles have to go through a rigorous peer review process before we can publish them." — (r/biology mtfkd, u/ImAJoVER — JoVE employee response)
- `[FABRICATED — reference example]` "The reviewers actually pushed back on our setup description — wanted us to re-shoot a section showing the buffer prep because the camera angle made it ambiguous. Took an extra month but the revised version is much clearer."

---

## 9. institutional_access — Institutional subscriptions, library budgets, cancellations

> Library-side subscription experience, institutional licensing pricing, budget cuts, cancellation announcements, price-hike complaints.

### Exemplars

- `[REAL]` "Apparently they increased their subscription cost by 45% two years in a row, which is pretty insane. I didn't check until recently, but we actually straight up unsubscribed this year." — (r/labrats 9hzpzh, u/lit0st)
- `[REAL]` "Access to this database will end due to budget cuts" — (r/uAlberta lxs135, u/WhyteFeline — post title; thread documents institutional cancellation context)
- `[REAL]` "Most of the time being on a university WiFi network gives you access to whatever they have access to." — (r/labrats q3nr48, u/Apprehensive-Pop3823)

---

## 10. methodology_focus — Methodology fit / cross-disciplinary applicability

> Whether JoVE is right for one's discipline or methodology, niche-protocol value, the "protocol library vs novel-science journal" framing.

### Exemplars

- `[REAL]` "JoVE isn't for novel science. It's a protocol library — obviously no Nobels are coming out of it, and it's not going to get the citations necessary for a high IF. I think it's an indispensable service all the same." — (r/labrats 9hzpzh, deleted user)
- `[REAL]` "Many of their protocols are just extremely niche. For example, I used a Jove protocol to measure heart rate in Drosophila larvae, and I can count on one hand the number of other groups that examine Drosophila larvae heart rate." — (r/labrats 9hzpzh, u/lit0st)
- `[FABRICATED — reference example]` "For systems biology where the experimental setup involves complex flow rates and chamber geometries, the video format is a real differentiator over text-only protocols — you can see the priming sequence, not just read about it."

---

## 11. indexing_impact — PubMed/MEDLINE indexing & impact factor

> Indexing in PubMed/MEDLINE, impact factor (~1.0 in 2024), citation expectations, what counts for tenure/promotion.

### Exemplars

- `[REAL]` "What? How can a journal with an impact factor of 1.2 have an expensive subscription plan?" — (r/labrats 9hzpzh, deleted user)
- `[REAL]` "IF is a great way to decide not if a paper is worth reading, but if a paper is worth believing. There's a hell of a lot more irreproducible papers in low-IF journals than in prestigious, high-IF journals" — (r/labrats 9hzpzh, deleted user)
- `[REAL]` "Their impact factor isn't a reflection of the quality of their content (it often isn't, for many journals). Many very prominent groups publish in Jove" — (r/labrats 9hzpzh, u/lit0st)

---

## 12. author_cv_value — Personal CV / career value of publishing in JoVE

> The author's own cost-benefit calculation: is publishing in JoVE worth it for MY career? Distinct from `reputation_with_pis` (which is advisor's verdict) — this is the individual's own math.

### Exemplars

- `[REAL]` "I was offered that as a late stage graduate student and it didn't seem worth the significant effort unless you had your own method to contribute." — (r/labrats 1s7vs2b, u/BoltVnderhuge)
- `[REAL]` "I got one in JoVE it was a cool experience and I am proud of the output" — (r/labrats 1s7vs2b, u/ragingbullfrog)
- `[REAL]` "Yep - legit. Though I'm not sure its worth the time and effort unless you really need some pubs." — (r/AskAcademia 29o6fu, u/mikemc43)

---

## 13. aggressive_marketing — Business-side sales/marketing aggression (not editorial outreach)

> JoVE's BUSINESS-side marketing/sales tactics: cold-call subscription pitches, spoofed librarian emails, mutating subscription packages, repeated contact attempts to non-decision-makers. Distinct from editorial outreach (themes 2 & 3).

### Exemplars

- `[REAL]` "They used my responses to spoof an email from me to my librarian requesting that we subscribe." — (r/AskAcademia 1nkbbm3, deleted user)
- `[REAL]` "I've had a jove marketer calling me multiple times a week trying to get me to buy access to their medical education thing for weeks. I keep insisting that I'm not the person at the university who makes that kind of purchases decision, but he doesn't seem to understand. It's bizarre." — (r/AskAcademia 1nkbbm3, u/fleemfleemfleemfleem)
- `[REAL]` "Librarians hate Jove for their extremely aggressive marketing, constantly mutating subscription packages, and inconsistent quality of their videos." — (r/AskAcademia 1nkbbm3, u/ZootKoomie)

---

## 14. production_experience — Author-side video production experience

> The hands-on author experience of making a JoVE video: lab visits by production crew, scripting demands, days of filming, professional quality of the production team.

### Exemplars

- `[REAL]` "They have an actual team come into your lab and professionally record you over several days. For methods papers I think the video format is great." — (r/AskAcademia 95pq3y, u/cag104)
- `[REAL]` "I have been approached by JoVE twice to publish video protocols. In one case we accepted (with a technique that was not previously published by them) and it was a lot of work and a good team and great experience." — (r/AskAcademia 95pq3y, u/alexa-488)
- `[REAL]` "The JoVE production folks did an awesome job. The most challenging part, to me, was making sure that the text, video, and voiceover were all totally consistent with each other" — (r/biology mtfkd, u/biobonnie)
- `[FABRICATED — reference example]` "The production schedule was tight — the JoVE team had a 4-day window for our shoot and we had to redo a transfection step on day 3 because our cells were less confluent than expected. They were patient about the reshoot."

---

## 15. peer_journal_comparison — Comparison to other journals (MDPI, Frontiers, Nature, Cell, etc.)

> JoVE discussed alongside or compared to other publishing outlets — both predatory-ecosystem peers (MDPI, Frontiers, Springer, Elsevier) and prestige journals (Nature, Cell). Frequently appears as a cross-tag rather than primary topic.

### Exemplars

- `[REAL]` "Like Springer and elsevier?" — (r/AskAcademia 1nkbbm3, u/lipflip)
- `[REAL]` "Also got similar e-mails from them, mdpi and frontiers." — (r/AskAcademia hjg41o, u/3d_extra)
- `[REAL]` "They're as bad as MDPI as far as I've seen." — (r/labrats 1s7vs2b, u/Sophsky)
- `[FABRICATED — reference example]` "Compared to publishing the same method in Nature Methods, JoVE has lower prestige but vastly better protocol-replication value for the kind of complex multi-step technique we developed."

---

## 16. reproducibility_value — Reproducibility benefit of video protocols

> JoVE's video format praised as a solution to research reproducibility problems — the idea that text protocols miss critical detail that video captures.

### Exemplars

- `[REAL]` "I think it's an innovative platform worth supporting (especially in light of some fields' reproducibility problems)." — (r/AskAcademia 95pq3y, u/Aubenabee)
- `[REAL]` "There's really no substitute for these videos, for some protocols — they are quite invaluable." — (r/labrats 9hzpzh, deleted user)
- `[REAL]` "Others in my department (zebrafish people) love it since it shows a real in-depth look at protocols as they are being performed in front of you, which is absolutely valuable to anyone." — (r/biology mtfkd, deleted user)
- `[FABRICATED — reference example, positive]` "We had three different labs successfully replicate our optogenetics setup within weeks of the JoVE video going live — that's a reproducibility win we wouldn't have gotten from a text paper."
- `[FABRICATED — reference example, negative]` "Even with the video, the reagent suppliers we showed are discontinued, so the protocol is hard to actually run as-shown. The video format doesn't fix the underlying problem of disappearing reagents."

---

## Versioning

- **v1** (2026-05-23): initial co-design with D Rage. 16 themes × 2–4 exemplars each. Real exemplars drawn from 15 validated Reddit threads (`data/seed-threads.json`). Fabricated reference examples authored only for thin-data themes (5, 8, 10, 14, 15, 16).
- Future revisions: append below with date, change summary, and the misclassification observed in production tagging that prompted the revision.
