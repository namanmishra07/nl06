---
title: "Robust PNT in contested environments"
date: 2026-04-28
source_type: paper
source_author: "Survey, IEEE Aerospace 2024"
blurb: "Multi-constellation receivers detect spoofing faster, but don't address the underlying problem."
draft: false
---

A survey paper, not new science, and it shows. The framing — combine GPS, Galileo, GLONASS, BeiDou in a single receiver to make spoofing harder — has been the consensus for half a decade. The contribution is the numbers: in their test environment, all-four-constellation receivers detect coherent multi-system spoof attacks ~3× faster than dual-system.

What's missing, and what I keep noticing in this literature: the threat model is always one spoofer transmitting from a known geometry. The harder operational case — several cheap SDRs spread over a port basin, each transmitting one constellation, hard to distinguish from real signals together — gets a paragraph in the future-work section.

The gap is independent verification. Multi-constellation in one receiver still trusts that receiver. Cross-receiver consensus would help, and that needs commercial-grade ground stations dense enough to triangulate. For now that's a research topic. It could become a market in about five years if maritime insurance requires it. That's the chain I'm tracking at [chains.](https://chains.nl06.com).
