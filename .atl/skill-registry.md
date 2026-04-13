# Skill Registry — IMAGIAX
_Generated: 2026-04-04 | Auto-built by sdd-init-proturbo_

## Agent Config Files (Project)
| File | Status |
|------|--------|
| AGENTS.md | ❌ Not found |
| CLAUDE.md | ❌ Not found |
| .cursorrules | ❌ Not found |

## Agent Config Files (User-level)
| File | Status |
|------|--------|
| ~/.config/opencode/AGENTS.md | ✅ Found — Senior Architect persona, Engram protocol, SDD skills auto-load |

---

## Available Skills

### User-level: ~/.config/opencode/skills/

| Skill | Trigger |
|-------|---------|
| branch-pr | Creating a PR, opening a pull request |
| copywriting | Marketing copy, rewrite pages, headlines |
| find-skills | "find a skill for X", "is there a skill that can..." |
| flutter-expert | Flutter 3+, Dart, Riverpod, GoRouter |
| go-testing | Go tests, Bubbletea TUI testing |
| issue-creation | Creating GitHub issues, reporting bugs |
| judgment-day | "judgment day", adversarial review, dual review |
| marketing-psychology | Psychology principles, behavioral science in marketing |
| pdf | PDF manipulation, form fill, extract tables |
| programmatic-seo | Pages at scale, location pages, SEO templates |
| remotion-best-practices | Remotion video creation in React |
| sdd-apply | Implement tasks from a change |
| sdd-archive | Archive a completed change |
| sdd-design | Write technical design document |
| sdd-explore | Investigate ideas, explore codebase |
| sdd-init | Initialize SDD context in a project |
| sdd-onboard | End-to-end SDD walkthrough |
| sdd-propose | Create a change proposal |
| sdd-spec | Write specifications (Given/When/Then) |
| sdd-tasks | Break down a change into tasks |
| sdd-verify | Validate implementation against specs |
| seo-audit | SEO audit, technical SEO review |
| skill-creator | Create new agent skills |

### User-level: ~/.agents/skills/

| Skill | Trigger |
|-------|---------|
| agent-browser | Browser automation, web testing, screenshots |
| algorithmic-art | Generative art, p5.js, flow fields |
| audit-website | Website audit, 230+ rules, squirrelscan |
| canvas-design | Poster, static design, .png/.pdf art |
| feedback-mastery | Difficult conversations, SBI feedback |
| find-skills | Discover installable skills |
| frontend-design | Web components, landing pages, dashboards |
| frontend-ui-ux-engineer | Stunning UI/UX without mockups |
| gemini | Gemini CLI, big context (>200k), code review |
| gsap-core | GSAP tweens, easing, matchMedia |
| gsap-frameworks | GSAP in Vue, Svelte, Nuxt |
| gsap-performance | Animation performance, 60fps, will-change |
| gsap-plugins | ScrollTrigger, Flip, Draggable, SplitText |
| gsap-react | GSAP in React/Next.js, useGSAP |
| gsap-scrolltrigger | Scroll-linked animations, pinning |
| gsap-timeline | gsap.timeline(), sequencing |
| gsap-utils | gsap.utils, clamp, mapRange |
| marketing-psychology | 70+ mental models for marketing |
| mermaid-diagrams | Class, sequence, flowchart, C4, ERD diagrams |
| paid-ads | Google Ads, Meta, LinkedIn campaigns |
| pricing-strategy | Pricing tiers, packaging, monetization |
| reducing-entropy | Minimize codebase size (manual-only) |
| seo-audit | SEO audit, meta tags, technical SEO |
| skill-creator | Create new AI skills |
| social-content | LinkedIn, Twitter, Instagram content |
| tiktok-app-marketing | TikTok/Instagram slideshow marketing |
| vercel-react-best-practices | React/Next.js performance, Vercel patterns |
| verification-before-completion | Run verification before claiming done |
| web-design-guidelines | UI review, accessibility, UX audit |

---

## Most Relevant Skills for IMAGIAX

| Task | Skill |
|------|-------|
| New feature proposal | sdd-propose |
| Write specs for a change | sdd-spec |
| Implement backend/frontend tasks | sdd-apply |
| Verify implementation | sdd-verify |
| Archive a completed change | sdd-archive |
| OHIF extension UI work | frontend-design / vercel-react-best-practices |
| Sequence diagrams (DICOM flow, auth) | mermaid-diagrams |
| PR creation | branch-pr |
| GitHub issue | issue-creation |
| Adversarial review | judgment-day |
| PDF report export (jsPDF in extension) | pdf |
| Website health check | audit-website |

---

## Active Changes (openspec/)

| Change | Path | Status |
|--------|------|--------|
| production-deployment | openspec/changes/production-deployment/ | 🟡 Proposal only (no spec/tasks) |
| security-audit | openspec/changes/security-audit/ + sdd/security-audit/ | 🟡 Exploration only |
| verification-fixes | openspec/changes/verification-fixes/ | 🔴 FAIL — T1.1 open (usuarios.activo) |

## Open Defects

| ID | Severity | Description |
|----|----------|-------------|
| T1.1 | CRITICAL | `/auth/verify` ignores `usuarios.activo=FALSE` — deactivated users can still authenticate |
