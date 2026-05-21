---
title: "Robust PNT in contested environments"
date: 2026-04-28
source_type: paper
source_author: "Survey, IEEE Aerospace 2024"
blurb: "Multi-constellation receivers shrink the spoofing detection window. Don't fix the deeper problem."
draft: false
---

A survey paper, not new science, and it shows. The framing — combine GPS, Galileo, GLONASS, BeiDou in a single receiver to make spoofing harder — has been the consensus for half a decade. The contribution is the numbers: in their test environment, all-four-constellation receivers detect coherent multi-system spoof attacks ~3× faster than dual-system.

What's missing, and what I keep noticing in this literature: the threat model is always *one* spoofer, transmitting from a known geometry. The actual operational threat — a fleet of cheap SDRs spread over a port basin, each transmitting one constellation, jointly indistinguishable from real — gets a paragraph in the future-work section.

The infrastructure gap is independent verification. Multi-constellation in one receiver still trusts the receiver. We need cross-receiver consensus, and that needs commercial-grade ground stations dense enough to triangulate. Currently a research curiosity. Plausibly a market in 5 years if maritime insurance mandates it. That's the chain I'm tracking at [chains.](https://chains.nl06.com).
