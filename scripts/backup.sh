#!/bin/sh
set -eu

: "${ATTACKER_BACKUP_ROOT:?ATTACKER_BACKUP_ROOT is required}"
: "${ATTACKER_DATABASE_URL_FILE:?ATTACKER_DATABASE_URL_FILE is required}"
: "${ATTACKER_DATA_DIR:?ATTACKER_DATA_DIR is required}"

backup_root="$(realpath "${ATTACKER_BACKUP_ROOT}")"
data_dir="$(realpath "${ATTACKER_DATA_DIR}")"
case "${backup_root}" in
    /|/app|/var|/var/lib) echo "refusing unsafe backup root" >&2; exit 1 ;;
esac
if [ ! -f "${ATTACKER_DATABASE_URL_FILE}" ]; then
    echo "database URL file is unavailable" >&2
    exit 1
fi
if [ ! -d "${data_dir}" ]; then
    echo "Attacker data directory is unavailable" >&2
    exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="${backup_root}/${timestamp}"
umask 077
mkdir -p "${destination}"

database_url="$(cat "${ATTACKER_DATABASE_URL_FILE}")"
pg_dump --dbname="${database_url}" --format=custom --no-owner --no-acl \
    --file="${destination}/database.dump"

if [ -d "${data_dir}/equipment-archive" ]; then
    tar -C "${data_dir}" -czf "${destination}/equipment-archive.tar.gz" equipment-archive
else
    tar -czf "${destination}/equipment-archive.tar.gz" --files-from /dev/null
fi

cat > "${destination}/metadata.json" <<EOF
{"format_version":1,"created_at":"${timestamp}","database_format":"pg_dump_custom","equipment_archive":"equipment-archive.tar.gz"}
EOF

(
    cd "${destination}"
    sha256sum database.dump equipment-archive.tar.gz metadata.json > SHA256SUMS
)

echo "backup created: ${destination}"
echo "retention is operator-controlled; no existing backup was deleted"
