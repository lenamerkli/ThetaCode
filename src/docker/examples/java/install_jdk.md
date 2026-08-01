For Debian and Ubuntu:
```bash
sudo apt update
sudo apt install -y wget apt-transport-https gpg
wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/adoptium.gpg > /dev/null
echo "deb https://packages.adoptium.net/artifactory/deb $(awk -F= '/^VERSION_CODENAME/{print$2}' /etc/os-release) main" | sudo tee /etc/apt/sources.list.d/adoptium.list
sudo apt update
```
Depending on the version you want to install:
```bash
sudo apt install -y temurin-25-jdk
```
```bash
sudo apt install -y temurin-21-jdk
```
```bash
sudo apt install -y temurin-17-jdk
```
