# nl06.com

Personal site. Multi-subdomain monorepo.

```
apps/
  main/      → nl06.com
  reading/   → reading.nl06.com
  chains/    → chains.nl06.com
packages/
  design-system/   → @nl06/design-system (shared)
```

## Develop

```
pnpm install
pnpm dev:main      # http://localhost:4321
pnpm dev:reading
pnpm dev:chains
```

## Build

```
pnpm build         # all apps
pnpm build:main    # one app
```
