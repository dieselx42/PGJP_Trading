# IBKR API: library choice and packaging

## Summary

The adapter in `app/broker/ibkr_broker.py` targets the **official Interactive
Brokers TWS API** (`ibapi`: `EClient` / `EWrapper`), per the brief's instruction
not to reach for a convenient-but-abandoned wrapper.

There is one real problem with that choice, and it is a packaging problem rather
than a technical one. **I have not installed anything. Read this and tell me
which option you want.**

---

## The problem

Interactive Brokers distributes the Python API as part of the **TWS API
installer** from `interactivebrokers.github.io`. They do **not** publish it to
PyPI themselves.

There *is* a package called `ibapi` on PyPI, and it does contain IBKR's source.
But it is a third-party redistribution: it is not published by Interactive
Brokers, its version cadence lags the official releases, and installing it means
trusting an uploader who is not the vendor for code that talks to a brokerage
account.

For most dependencies that is an acceptable everyday risk. For the one component
that can place trades, it deserves a decision rather than a default — which is
why `pyproject.toml` has it as an optional extra that nothing installs
automatically:

```toml
[project.optional-dependencies]
ibkr = ["ibapi>=10.30.1"]
```

`disabled` and `mock` modes never import it. The module is written so it imports
cleanly when the package is absent, and `connect()` refuses with an explanatory
message rather than an `ImportError`.

---

## Options

### Option A — Vendor the official source (recommended)

Download the TWS API from IBKR directly and install from the extracted source:

```bash
# On the VPS, from the official IBKR download
wget https://interactivebrokers.github.io/downloads/twsapi_macunix.<version>.zip
unzip twsapi_macunix.<version>.zip
cd IBJts/source/pythonclient
pip install .
```

**For:** authentically IBKR's code, from IBKR, with a checksum you can verify
against their site. No third party in the supply chain.

**Against:** the download URL and version have to be updated by hand; it is not
a `pip install -r requirements.txt` away; the Docker build needs the archive
present in the build context or fetched at build time.

**My recommendation.** For the single component that can move money, a manual
step you do once per upgrade is a good trade for removing a third party from the
chain. It also fits how this project already works — the runtime has no
dependencies at all today.

### Option B — Install the PyPI redistribution

```bash
pip install -e '.[ibkr]'
```

**For:** one command; works in CI and Docker with no special handling; the
package is widely used.

**Against:** an uploader who is not Interactive Brokers sits between you and the
code that places your orders.

If you choose this, pin an exact version and a hash:

```
ibapi==10.30.1 --hash=sha256:<hash>
```

### Option C — `ib_async`

`ib_async` is the actively maintained community fork of `ib_insync` (whose
author died in 2024, after which the original project was archived). It is a
genuinely nicer API — native asyncio, less boilerplate — and it is not abandoned.

**Against:** it is still a third-party wrapper over the official API, which is
exactly what the brief asked me not to pick by default. It would also mean
rewriting `ibkr_broker.py`.

**Not recommended without your explicit approval**, but worth knowing about: if
the raw `EClient`/`EWrapper` callback model proves painful in practice, this is
the escape hatch, and the `Broker` abstraction means only one file changes.

---

## What the adapter does about API instability

`ibapi`'s `EWrapper.error` signature changed in 10.30 — an `errorTime` parameter
was inserted ahead of `errorCode`. Rather than pin a version and break on
upgrade, the adapter identifies the arguments by shape:

```python
def error(self, *args: Any, **kwargs: Any) -> None:
    req_id, code, message = _parse_error_args(args, kwargs)
```

`_parse_error_args` handles the pre-10.30 signature, the 10.30+ signature, and
the keyword form. It is unit tested for all three.

---

## Verification status

**Verified against a real IB Gateway paper session on 2026-08-08** — ibapi
10.30.1, server version 187, account `DU***787`, via `app.cli ibkr-checkout`.

| Verified against a live gateway | Still unverified |
|---|---|
| Socket connect and handshake | Order placement |
| `managedAccounts` / `nextValidId` timing | Order status callbacks |
| Account-type detection from a real `DU` id | Fills and executions from our own orders |
| Account summary | Commission reports |
| Positions | `get_open_orders` (see below) |
| Executions (empty, but the call completes) | Market data ticks (see below) |
| Contract qualification against live `contractDetails` | |
| Error classification, against codes 321, 200 and 354 | |

Contract facts confirmed for MSL: multiplier `25`, min tick `0.05`, trading
class `MSL`, exchange `CME`, timezone `US/Central`. IBKR lists expirations as
full last-trade dates (`20260828`), not months, and quotes ten contracts —
monthly through Jan 2027, quarterly after.

### Two probes that cannot pass in this configuration

**`OPEN_ORDERS_EMPTY`.** IB Gateway's Read-Only API mode refuses `reqOpenOrders`
with code 321 and `reqId` `-1`. That is IBKR's behaviour, not a defect, but it
means `get_open_orders` — and therefore order reconciliation — cannot be
exercised until Read-Only mode is turned off at RUNBOOK step 10.

**`MARKET_DATA`.** Code 354: the paper account has no live CME futures
subscription, and IBKR offers delayed data instead. See
`REASON_MARKET_DATA_DELAYED` — the system refuses to trade on delayed prices and
there is no setting that permits it.

### What the first real session found

Three defects that unit tests against fakes could not have found, each the same
shape: **IBKR said exactly what was wrong and the adapter turned it into an
absence.**

1. **An error with `reqId` `-1` orphaned its pending request.** A 321 refusal
   matched neither the reject-by-id path nor the fail-everything path, so the
   request waited out its full 20-second timeout and reported only how long it
   had waited. Untargeted errors are now recorded and attached to whatever times
   out during their window — without ever being attributed to a specific
   request, because failing the wrong one would be worse.

2. **A refused market-data subscription returned an empty tick.** Streaming
   requests register no future, so `_reject` found nothing to fail and dropped
   the error. An empty tick from a refusal was byte-identical to one from a
   quiet market.

3. **Delayed prices were indistinguishable from live ones.** Tick types 66/67/68
   were mapped into the same `bid`/`ask`/`last` fields as 1/2/4. Every freshness
   check would have passed — the tick did arrive a second ago; only the price in
   it was fifteen minutes old.

The third is the one to remember. It was not a bug in anything that had been
written; it was a hazard that only existed once a real broker was on the other
end of the socket.

---

## Design notes on the adapter

**Everything IBKR lives in one file.** `app/broker/ibkr_broker.py` is the only
module that imports `ibapi`. The rest of the system talks to the abstract
`Broker` and the broker-neutral models in `app/broker/models.py`.

**Threads bridged to asyncio.** `ibapi` runs a reader thread and delivers
callbacks on it. The adapter hands results back to the asyncio side through
`concurrent.futures.Future`, which is thread-safe by design, and wraps them with
`asyncio.wrap_future`.

**Account identity from the broker.** Paper accounts carry a `D` prefix
(`DU`/`DF`/`DI`), live accounts start with `U`. Anything else is `UNKNOWN`,
which never trades. A session managing a mix of paper and live accounts is also
`UNKNOWN`. The adapter **disconnects** if the observed type does not match the
configured mode.

**Futures permission is reported as unknown.** The TWS API exposes no flag for
"is this account permitted to trade CME futures". Rather than infer one,
`AccountSummary.futures_permission` is `None`, which the gate treats as *not
permitted*. The operator establishes permission out of band and records it in
`SOL_FUTURES_PERMISSION_READY`.

**Permission errors are never retried.** They are classified as non-retryable
and surfaced as configuration problems. Retrying a refusal the account cannot
satisfy is how a bot ends up hammering the broker. Unknown error codes also
default to non-retryable, for the same reason.

**Only market and limit orders transmit.** Stop, stop-limit, and bracket orders
are modelled and persisted, but `place_order` refuses them with
`BrokerUnsupportedOperationError`. Widening `TRANSMITTABLE_ORDER_TYPES` is a
deliberate change that must come with its own tests.

---

## IB Gateway operational note

IB Gateway requires an interactive login and 2FA, and re-authenticates on its
own schedule. Running it unattended usually means a third-party supervisor such
as **IBC** (IBController).

**Nothing of the sort has been installed, and I will not install one without
your approval.** It is a third-party component sitting in the authentication
path of a brokerage account, which is your decision to make, not a sensible
default. Until you decide, expect to authenticate IB Gateway manually.

If you do want it, the trade-offs to weigh are:

- IBC stores or supplies the IBKR password to automate login. That is precisely
  the credential handling this project otherwise avoids entirely.
- It is widely used and open source, so it is auditable.
- The alternative is a manual login whenever the gateway restarts, which is
  workable for paper trading and a real operational burden for anything running
  continuously.
