# Base image for scoring runs: ob + conda + denet, nothing else. Methods bring their own conda envs.
# podman build -t sc-brrr-base:0.7.0 .
FROM debian:bookworm-slim

# git: ob clones module repos; ca-certificates: HTTPS for the setup step
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG MINIFORGE=24.11.3-0
ADD https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE}/Miniforge3-${MINIFORGE}-Linux-x86_64.sh /tmp/miniforge.sh
RUN bash /tmp/miniforge.sh -b -p /opt/conda && rm /tmp/miniforge.sh \
 && /opt/conda/bin/conda clean -afy
ENV PATH=/opt/conda/bin:$PATH

# ob in its own env so the base env stays the conda that snakemake drives;
# denet here is the trusted copy (a module env can bring its own)
RUN conda create -y -n ob python=3.13 denet=0.10.3 \
 && conda run -n ob pip install --no-cache-dir omnibenchmark==0.7.0 \
 && conda clean -afy
# base first (conda), then the ob env (ob, snakemake)
ENV PATH=/opt/conda/bin:/opt/conda/envs/ob/bin:$PATH

WORKDIR /bench
