# Request-to-product acceptance teardown

The current request-first gate makes no external provider changes. Its disposable
request-management and product-publication SQLite files are confined to the owner-only root created
during setup. There is no repository-owned or provider resource to delete after the focused gate.

Inspect the exact root and confirm that the checkout contains no generated acceptance database or
evidence file:

```sh
set -eu
test -n "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT"
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  /*) ;;
  *) exit 1 ;;
esac
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  "$PWD"|"$PWD"/*) exit 1 ;;
esac
test "$(stat -f '%Lp' "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT")" = 700
test -f "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT/.pillarmesh-request-product-run"
find "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" -maxdepth 4 -print
git status --short
find . -type f \( -name 'requests.sqlite3' -o -name 'product-publications.sqlite3' \) -print
```

After inspection confirms that the root contains only this run's disposable files, remove that exact
root:

```sh
set -eu
test -n "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT"
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  /*) ;;
  *) exit 1 ;;
esac
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  "$PWD"|"$PWD"/*) exit 1 ;;
esac
test "$(stat -f '%Lp' "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT")" = 700
test -f "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT/.pillarmesh-request-product-run"
rm -rf -- "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT"
unset PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT
```

Do not run the removal when the variable is empty, the directory is shared, or any entry is
unrecognized. Do not delete another run's workspace. Preserve unrelated and pre-existing changes in
the checkout.

## Live resource cleanup is not yet available

The complete Task 16 live runner and its exact-resource ledger do not exist. Consequently, there is
no authorized unified teardown command for a request-to-product live run. Do not substitute broad
Docker cleanup, provider-wide deletion, schema wildcards, catalog searches, Superset title searches,
or recursive filesystem removal.

Component live tests own only the resources created inside their own fixture and cleanup boundary.
For example, the PostgreSQL materialization, Superset dashboard, request-to-dashboard, and managed
warehouse lifecycle suites each prove and clean their own narrower transaction. Run their documented
or fixture-owned cleanup as part of that same test; never infer authority over resources created by
a different run.

A future composed live teardown must read a private, authenticated ledger containing every exact
source object, warehouse relation, catalog entity, dashboard object, access principal, state store,
backup, container, network, volume, and evidence path created for one correlation identifier. It
must respect retention, prove provider absence after cleanup, be replay safe, and fail closed when
the ledger or run identity is missing or inconsistent. Until that authority ships, a failed composed
experiment must be handled by resource-by-resource review rather than claimed as Task 16 acceptance.
