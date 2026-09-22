# Build policy for Docker Buildx (auto-loaded alongside Dockerfile).
#
# Why this file exists: recent Buildx versions automatically load a policy
# file matching the Dockerfile name, and when the build context is remote
# (e.g. `build: https://github.com/...git` in docker-compose.yml) it fetches
# this file from that same remote context. If the file is absent the fetch
# 404s and the whole build fails with:
#   "failed to stat policy file Dockerfile.rego"
# This intentionally permissive policy is equivalent to "no policy" — it
# exists so remote-context builds proceed instead of failing on the lookup.
package docker

default allow := true

decision := {"allow": allow}
