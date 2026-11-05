#!/usr/bin/env bash
# FILE: scripts/setup_host.sh
# RUOLO NEL PROGETTO: Installazione automatica dei tool host su Debian 13 (richiede sudo).
#
# Installa: Docker CE, Docker Scout CLI plugin, kubectl, kind, wrk, python3-venv.
# Aggiunge inoltre l'utente corrente al gruppo docker per consentire l'uso senza sudo.
# Questo script non è un installer Linux universale: usa APT/systemd, configura il
# repository Debian "trixie" e scarica il binario KinD per Linux amd64. Per macOS
# usare setup_host_macos.sh; per Windows usare setup_host.ps1.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[-] Questo script deve essere eseguito con i privilegi di root:"
    echo "    sudo bash scripts/setup_host.sh"
    exit 1
fi

ACTUAL_USER="${SUDO_USER:-$USER}"
USER_HOME=$(getent passwd "$ACTUAL_USER" | cut -d: -f6)

echo "[+] Account configurato: $ACTUAL_USER (Cartella: $USER_HOME)"

# 1. Installazione pacchetti di sistema di base e wrk
echo "[+] Step 1: Installazione pacchetti di sistema (wrk, python3-venv, ca-certificates)..."
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    wrk \
    python3-venv \
    python3-pip \
    jq

# 2. Configurazione del repository ufficiale di Docker per Debian
echo "[+] Step 2: Configurazione del repository ufficiale Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# "trixie" identifica Debian 13; il repository va sostituito se si usa una
# versione Debian diversa. L'architettura APT viene rilevata, ma il binario KinD
# scaricato sotto è esplicitamente amd64.
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# 3. Aggiunta dell'utente al gruppo docker
echo "[+] Step 3: Aggiunta dell'utente '$ACTUAL_USER' al gruppo docker..."
usermod -aG docker "$ACTUAL_USER"

# 4. Installazione del plugin Docker Scout CLI per l'analisi CVE (Punti D ed E)
echo "[+] Step 4: Installazione di Docker Scout..."
mkdir -p /usr/local/libexec/docker/cli-plugins
curl -fsSL https://raw.githubusercontent.com/docker/scout-cli/main/install.sh | sh -s -- -b /usr/local/libexec/docker/cli-plugins
chmod +x /usr/local/libexec/docker/cli-plugins/docker-scout || true

mkdir -p "$USER_HOME/.docker/cli-plugins"
ln -sf /usr/local/libexec/docker/cli-plugins/docker-scout "$USER_HOME/.docker/cli-plugins/docker-scout"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$USER_HOME/.docker"

# 5. Configurazione globale di kubectl e kind in /usr/local/bin
echo "[+] Step 5: Verifica disponibilità globale di kubectl e kind..."
if [ ! -f /usr/local/bin/kubectl ]; then
    # La versione di kubectl segue il canale stabile pubblicato da Kubernetes;
    # il binario scaricato è per Linux amd64.
    KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)
    curl -Lo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
    chmod +x /usr/local/bin/kubectl
fi

if [ ! -f /usr/local/bin/kind ]; then
    # KinD è fissato a v0.25.0 per rendere ripetibile la configurazione del corso.
    curl -Lo /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/v0.25.0/kind-linux-amd64"
    chmod +x /usr/local/bin/kind
fi

# 6. Avvio e abilitazione del demone Docker
echo "[+] Step 6: Avvio del demone Docker..."
# systemctl è disponibile nei sistemi Linux con systemd; l'installer è quindi
# destinato a Debian compatibile con systemd, non a distribuzioni minimal/container.
systemctl enable docker
systemctl restart docker

echo "[✓] Configurazione completata con successo! Per applicare i permessi docker nella sessione terminale corrente: newgrp docker"
