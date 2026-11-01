# The CD agent: docker CLI + compose to drive the VM's daemon, git to pull the
# clone it deploys from, curl+jq to ask GitHub whether CI is green, bash for a
# script with pipefail, python3 for a product's POST_DEPLOY_HOOK (standard
# library only). Pinned major to match the daemon's era.
# Pinned by digest (docker:29.9.2-cli, compose v5.6.1 - the version whose
# --remove-orphans/profile behavior was empirically verified): a floating tag
# could pick up a compose that treats the profiled deployer as an orphan.
FROM docker:29.9.2-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb
RUN apk add --no-cache git jq curl bash python3
COPY deploy-loop.sh /deploy-loop.sh
# ci-policy.json is rendered beside these by deploy-loop.sh at start.
COPY ci-gate.sh ci-gate.jq /
ENTRYPOINT ["/bin/bash", "/deploy-loop.sh"]
