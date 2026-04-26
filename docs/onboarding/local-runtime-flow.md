# Local Runtime Flow (Current Verified State)

**Classification:** INTERNAL / NON-PRODUCTION  
**Change:** `local-share-onboarding-package`  
**Evidence basis:** `docs/onboarding/local-baseline-verification.md` (EV-002..EV-016)

---

## 1) Current-state flow (observed baseline)

```mermaid
flowchart TD
    A[Coworker / Maintainer<br/>Windows local machine] --> B[INICIAR-IMAGIAX.bat]
    B --> C[start-imagiax.ps1]

    C --> D[Docker Engine check/start]
    D --> E[docker compose up -d<br/>orthanc-imagiax stack]

    E --> O[Orthanc container :8042/:4242]
    E --> P[Postgres container :5432]

    O -. current blocker .-> X[Bind error on 0.0.0.0:4242<br/>EV-002]
    X -. cascade .-> N[Nginx instability: upstream orthanc missing<br/>EV-005]
    X -. cascade .-> H[OHIF not reachable on :3000 in this run<br/>EV-003]

    C --> F[FastAPI check/start]
    F --> G[/ping = 200 (liveness)<br/>EV-006, EV-012]
    F --> I[/salud = 401 (auth-protected)<br/>EV-006, EV-012]

    P --> Q[psql select 1 = success<br/>EV-007]

    C --> U[upload-dicoms.ps1 -DryRun (optional)]
    U --> V[Default paths now repo-relative<br/>EV-011]
    U --> W[Upload blocked if Orthanc unreachable<br/>EV-008]
```

---

## 2) Notes for readers

- The launcher parse failure with parenthesis paths was fixed in current repo behavior (EV-010).
- The main unresolved environment blocker remains Orthanc host bind on port `4242` (EV-002).
- Because of that blocker, this flow represents **partial startup capability**, not full local success.
- Nginx shown here is baseline-context only; in current local `orthanc-imagiax/docker-compose.yml`, nginx is not a declared service and may appear only as orphan/leftover container state from prior stacks.

---

## 3) Scope boundary reminder

- This diagram is local-onboarding documentation only.
- It does not describe production topology/runbooks.
- It does not imply any AI clinical logic behavior change.
