#!/bin/sh
set -eu

: "${ATTACKER_RESTORE_CONFIRM:?set ATTACKER_RESTORE_CONFIRM=restore-empty-target}"
: "${ATTACKER_RESTORE_SOURCE:?ATTACKER_RESTORE_SOURCE is required}"
: "${ATTACKER_DATABASE_URL_FILE:?ATTACKER_DATABASE_URL_FILE is required}"
: "${ATTACKER_DATA_DIR:?ATTACKER_DATA_DIR is required}"

if [ "${ATTACKER_RESTORE_CONFIRM}" != "restore-empty-target" ]; then
    echo "restore confirmation did not match" >&2
    exit 1
fi

source_dir="$(realpath "${ATTACKER_RESTORE_SOURCE}")"
data_dir="$(realpath "${ATTACKER_DATA_DIR}")"
case "${source_dir}" in
    /|/app|/var|/var/lib) echo "refusing unsafe restore source" >&2; exit 1 ;;
esac
case "${data_dir}" in
    /|/app|/var|/var/lib) echo "refusing unsafe data directory" >&2; exit 1 ;;
esac
for required in SHA256SUMS database.dump equipment-archive.tar.gz metadata.json; do
    if [ ! -f "${source_dir}/${required}" ]; then
        echo "backup is missing ${required}" >&2
        exit 1
    fi
done
if [ ! -d "${data_dir}" ]; then
    echo "restore data directory must already exist" >&2
    exit 1
fi
if find "${data_dir}" -mindepth 1 -print -quit | grep -q .; then
    echo "restore data directory must be empty" >&2
    exit 1
fi

(
    cd "${source_dir}"
    sha256sum -c SHA256SUMS
)

if tar -tzf "${source_dir}/equipment-archive.tar.gz" \
    | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then
    echo "equipment archive contains an unsafe path" >&2
    exit 1
fi

database_url="$(cat "${ATTACKER_DATABASE_URL_FILE}")"
table_count="$(psql "${database_url}" -Atc \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';")"
if [ "${table_count}" != "0" ]; then
    echo "restore database must be empty" >&2
    exit 1
fi

pg_restore --dbname="${database_url}" --no-owner --no-acl --exit-on-error \
    "${source_dir}/database.dump"
tar -C "${data_dir}" -xzf "${source_dir}/equipment-archive.tar.gz"

echo "restore completed from ${source_dir}"
