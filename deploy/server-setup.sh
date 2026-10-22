#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server (Oracle Cloud Ampere/Arm) for LeadFlow AI.
# Run it on the server as the default "ubuntu" user:   bash server-setup.sh
# Safe to run again: every step checks before it changes anything.
set -euo pipefail

echo "==> System updates"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q upgrade
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install ca-certificates curl unattended-upgrades netfilter-persistent iptables-persistent

echo "==> Docker Engine and the compose plugin (official Docker repository)"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

echo "==> Firewall: allow HTTP and HTTPS (Oracle's Ubuntu image rejects everything except SSH)"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
    reject_line=$(sudo iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')
    if [ -n "$reject_line" ]; then
      sudo iptables -I INPUT "$reject_line" -p tcp --dport "$port" -m state --state NEW -j ACCEPT
    else
      sudo iptables -A INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT
    fi
  fi
done
sudo netfilter-persistent save

echo "==> Automatic security updates"
echo 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null

echo "==> SSH: keys only, no passwords, no root login"
sudo sed -i -E 's/^#?PasswordAuthentication .*/PasswordAuthentication no/; s/^#?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl try-reload-or-restart ssh  # Ubuntu 24.04 starts sshd on demand (ssh.socket)

echo
echo "Done. Log out and back in (so the docker group applies), then deploy LeadFlow AI."
