# ==============================================================================
# STAGE 1: Builder (Compiles ISCE3, Setups Envs)
# ==============================================================================
FROM nvidia/cuda:12.8.1-devel-oraclelinux9 AS builder

USER root

ENV HOME=/home/jovyan \
    CONDA_DIR=/opt/conda \
    PATH=/opt/conda/bin:$PATH \
    ISCE3_INSTALL_DIR=/opt/isce3

# 1. Install System Build Dependencies
RUN set -ex \
    && yum update -y \
    && yum install -y curl bzip2 zip unzip yum-utils which file make \
       autoconf openssh-clients git gcc-c++ \
    && yum clean all \
    && rm -rf /var/cache/yum

# 2. Install Mamba
RUN curl -sSL https://github.com/conda-forge/miniforge/releases/download/24.7.1-0/Mambaforge-24.7.1-0-Linux-x86_64.sh -o miniconda.sh \
    && bash miniconda.sh -b -p /opt/conda \
    && rm miniconda.sh \
    && mamba init bash

WORKDIR $HOME

# 3. Copy and setup installation script and version file
COPY install-isce3.py $HOME/
COPY docker/isce3-version.txt $HOME/
RUN chmod +x $HOME/install-isce3.py

# 4. Install base conda dependencies needed by script
RUN mamba install -n base -y -c conda-forge \
    nb_conda_kernels ipykernel papermill git cmake make kernda \
    && mamba clean --all -f -y

# 5. Run ISCE3 installation (clones repo, creates env, builds, installs)
RUN $HOME/install-isce3.py \
    --commit-hash $(cat $HOME/isce3-version.txt) \
    --repo-dir $HOME/isce3 \
    --envs-dir /opt/conda/envs \
    --env-name isce3_src \
    --build-dir $HOME/isce3/build \
    --cmake-build-type Release \
    --make-jobs 4 \
    --assume-yes

# 6. Install Notebook Wrapper (IN BOTH ENVS)
RUN set -ex \
    && git clone https://github.com/hysds/notebook_pge_wrapper.git $HOME/notebook_pge_wrapper \
    && cd $HOME/notebook_pge_wrapper \
    && mamba run -n base pip install . \
    && mamba run -n isce3_src pip install . \
    && mamba run -n isce3_src python -m ipykernel install --user --name isce3_src --display-name "isce3_src"

# 7. Install NISAR on-demand tooling (nisar_products: runconfig localizer, naming, S3).
#    Pulls in boto3/geopandas/numpy/pyyaml/shapely + earthaccess for netrc-based DAAC auth.
#    Vendored as a tarball in-repo (docker/ondemand-resources.tar.gz) so the build needs
#    no S3 credentials and no enterprise-GitHub auth. Contains only setup.py + src/.
COPY docker/ondemand-resources.tar.gz /tmp/ondemand-resources.tar.gz
RUN set -ex \
    && mkdir -p $HOME/ondemand-resources \
    && tar -xzf /tmp/ondemand-resources.tar.gz -C $HOME/ondemand-resources \
    && rm /tmp/ondemand-resources.tar.gz \
    && mamba run -n isce3_src pip install "$HOME/ondemand-resources[earthaccess]"

# 8. Re-patch the isce3_src kernel so its kernel.json carries the ISCE3 build env vars
#    (PYTHONPATH/LD_LIBRARY_PATH). Step 6's ipykernel re-registration dropped kernda's
#    injection; without this, notebooks run via papermill can't import nisar/isce3.
RUN mamba run -n base kernda -o -y \
    --env-dir /opt/conda/envs/isce3_src \
    /home/jovyan/.local/share/jupyter/kernels/isce3_src/kernel.json


# ==============================================================================
# STAGE 2: Final Runtime
# ==============================================================================
FROM nvidia/cuda:12.8.1-base-oraclelinux9

USER root

ENV HOME=/home/jovyan \
    CONDA_DIR=/opt/conda \
    PATH=/opt/conda/bin:$PATH \
    ISCE3_INSTALL_DIR=/home/jovyan/isce3/build \
    NOTEBOOK_PGE_DIR=/home/jovyan/nisar-isce3-pge/notebook_pges \
    NISAR_ONDEMAND_SRC=/home/jovyan/ondemand-resources/src

# 1. Setup User and Runtime Deps
RUN set -ex \
    && useradd -m -s /bin/bash -N -u 1006 -r -d /home/jovyan jovyan \
    && echo "jovyan ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && yum update -y \
    && yum install -y curl sudo less unzip git openssh-clients \
    && yum clean all \
    && rm -rf /var/cache/yum

# 2. COPY Artifacts from Builder
# This copies BOTH environments (base and isce3_src) because they live inside /opt/conda
COPY --from=builder /opt/conda /opt/conda
# ISCE3 installs into its build tree, not /opt/isce3. Copy the runtime pieces
# (python packages + shared libs) to the SAME path the isce3_src env vars and the
# kernda-patched kernel.json expect: /home/jovyan/isce3/build/{packages,lib64}.
COPY --from=builder /home/jovyan/isce3/build/packages /home/jovyan/isce3/build/packages
COPY --from=builder /home/jovyan/isce3/build/lib64 /home/jovyan/isce3/build/lib64
COPY --from=builder /home/jovyan/.local /home/jovyan/.local
# nisar_products source (also pip-installed into isce3_src) for reference / NISAR_ONDEMAND_SRC.
COPY --from=builder /home/jovyan/ondemand-resources /home/jovyan/ondemand-resources

# 3. Copy Application Code
COPY . $HOME/nisar-isce3-pge

# 4. Register /opt/conda/envs so `conda run -n isce3_src` resolves by NAME.
# The env is built at /opt/conda/envs/isce3_src, but `-n` resolution scans conda's
# envs_dirs, which is a property of the runtime conda root + HOME + condarc -- not of
# this image. install-isce3.py's `conda config --append envs_dirs` wrote to the
# BUILDER's ~/.condarc, which is not copied here, so on a HySDS worker envs_dirs[0]
# is ~/.conda/envs and `-n isce3_src` fails with EnvironmentLocationNotFound. Write
# the entry into the runtime user condarc ($HOME/.condarc): it is HOME-based, and the
# worker's HOME is /home/jovyan (that is where the failing lookup rooted), so conda
# reads it regardless of conda root. The notebook still probes this and falls back to
# `-p` if it did not take (see the run-sas cell).
RUN conda config --append envs_dirs /opt/conda/envs \
    && conda config --show envs_dirs

# 5. Final Permissions and Env
RUN chown -R jovyan:users $HOME \
    && chmod -R 777 $HOME \
    && echo ". /opt/conda/etc/profile.d/conda.sh" >> $HOME/.bash_profile \
    && echo "conda activate isce3_src" >> $HOME/.bash_profile \
    && mkdir -p $HOME/.cache && chmod 777 $HOME/.cache

ENV PYTHONPATH="${NOTEBOOK_PGE_DIR}:${ISCE3_INSTALL_DIR}/packages:${PYTHONPATH}" \
    LD_LIBRARY_PATH="${ISCE3_INSTALL_DIR}/lib64:${LD_LIBRARY_PATH}"

WORKDIR $HOME
USER jovyan
CMD ["/bin/bash", "--login"]