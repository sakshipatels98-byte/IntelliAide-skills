# Build the IntelliAide skills image.
#
# Strategy:
#   Base image  = registry.access.redhat.com/ubi9/python-312
#                 Standard Red Hat UBI9 Python image. No external image
#                 dependencies. Fully reproducible from public sources.
#
#   Source      = intelliaide/ folder in this repository.
#                 All IntelliAide engine code (Main-program/, python-client/,
#                 Machine-learning/, Config/, DataSource/) AND the four skill
#                 wrapper scripts live here — no private images required.
#
# Layout inside the final image:
#   /intelliaide/
#     CLAUDE.md                        Orchestration instructions for Claude
#     extract_cluster.py               Step 1: live cluster extraction
#     select_files.py                  Step 2: LLM-based file selection
#     analyze_data.py                  Step 3: ML YAML + log analysis
#     perform_rca.py                   Step 4: LLM-based RCA
#     app_paths.py                     Path helpers (auto-resolves to /app/skills/intelliaide/)
#     requirements.txt
#     Main-program/                    IntelliAide core engine
#     python-client/                   Kubernetes live-extraction helpers
#     Machine-learning/                Drain3 + YAML ML classifiers
#     Config/                          config.json, yaml_processing.yaml, etc.
#     DataSource/                      MUST_GATHER_*.md topology docs
#
# WHY /intelliaide/ at image root:
#   The SandboxTemplate mounts the skills image volume at /app/skills/ in the
#   sandbox container. Kubernetes image volumes map the image root (/) to the
#   mountPath, so:
#     image /intelliaide/ → container /app/skills/intelliaide/  ✓
#
# To build:
#   podman build -f Containerfile -t <registry>/lightspeed-skills:latest .
#
# To build via OpenShift BuildConfig:
#   oc start-build lightspeed-skills -n openshift-lightspeed \
#     --from-dir=. --follow

FROM registry.access.redhat.com/ubi9/python-312

USER root

# Copy the entire intelliaide skill folder into the image root
COPY intelliaide/ /intelliaide/

# Vendor all Python dependencies into /intelliaide/vendor/.
# The skills image filesystem is mounted into the sandbox pod at /app/skills/,
# but the sandbox runs scripts with its OWN Python interpreter — not the UBI9
# Python in this image.  Installing to the UBI9 site-packages would therefore
# have no effect at runtime.  Vendoring into /intelliaide/vendor/ ensures the
# packages travel with the scripts and can be found via PYTHONPATH.
RUN pip install --no-cache-dir --target /intelliaide/vendor -r /intelliaide/requirements.txt

# Ensure skill scripts are executable and permissions are OpenShift-compatible.
# chgrp 0 / g+rwX is the standard pattern for arbitrary UID support in OpenShift.
RUN chmod -R 755 /intelliaide && \
    mkdir -p /tmp/intelliaide /tmp/cluster-extract /tmp/intelliaide-app/Results && \
    chgrp -R 0 /intelliaide /tmp/intelliaide /tmp/cluster-extract /tmp/intelliaide-app && \
    chmod -R g+rwX /intelliaide /tmp/intelliaide /tmp/cluster-extract /tmp/intelliaide-app

# Drop back to unprivileged user (OpenShift requires non-root)
USER 1001
