#!/usr/bin/env bash
#
# Sourceable digest-resolution helpers (Epic 81, digest-capture fix).
#
# `docker inspect --format='{{index .RepoDigests 0}}'` returns the SOURCE
# image's digest, which for a multi-arch image is the manifest-list/index
# digest — not the single-platform manifest digest the registry actually
# stored under a given tag. These helpers resolve the digest the registry
# itself reports instead.
#
# No `set -e`/trap/top-level side effects here — safe to `source` from any
# caller regardless of that caller's own shell options.

# parse_push_digest — read `docker push` output from stdin, print the last
# 'digest: sha256:<hex>' value found, or nothing if no such line exists.
parse_push_digest() {
  local last="" line
  while IFS= read -r line; do
    if [[ "${line}" =~ digest:[[:space:]]*(sha256:[0-9a-fA-F]+) ]]; then
      last="${BASH_REMATCH[1]}"
    fi
  done
  printf '%s' "${last}"
}

# resolve_manifest_digest_via_http <domain> <repo_path> <tag> <user> <pass>
# — query the registry's v2 manifest endpoint directly and print the
# Docker-Content-Digest response header (the digest the registry stored for
# that tag), or nothing if the header is absent.
resolve_manifest_digest_via_http() {
  local domain="$1" repo_path="$2" tag="$3" user="$4" pass="$5"
  curl -sI -u "${user}:${pass}" \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    "https://${domain}/v2/${repo_path}/manifests/${tag}" \
    | tr -d '\r' \
    | grep -i '^docker-content-digest:' \
    | sed -E 's/^[^:]+:[[:space:]]*//' \
    | tail -n1
}
