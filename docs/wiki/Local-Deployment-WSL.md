# Local Deployment (WSL / Linux)

The `dev-setup.sh` script automates the installation of a local single-node Kubernetes cluster (K3s), builds the application images, and provisions the Odoo SaaS MVP architecture natively on your dev machine. It is specifically optimized for Ubuntu and WSL (Kali Linux, Ubuntu, etc).

## Prerequisites
- **Docker**: Must be installed and running.
- **Docker Permissions**: Your user must have permission to access the Docker daemon socket (usually by being in the `docker` group or having rw access to `/var/run/docker.sock`).
- **sudo/root privileges**: Required to install K3s.

> **Note for WSL Users**: 
> If you encounter `Cannot connect to the Docker daemon` when running the script, execute `sudo chmod 666 /var/run/docker.sock` to temporarily grant access to the socket within your WSL session, then run the script again.
> If the script hangs continuously asking for the `[sudo] password for <user>`, you can temporarily grant passwordless sudo by running: `echo "kali ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/nopasswd` (replace `kali` with your username).

## Running the Setup

The script supports dynamic environment variables so you don't have to define your dev credentials in the repository files manually.

If you run the script directly, it will use these safe **default credentials**:
- **DB_PASSWORD**: `DevPass2026!`
- **ADMIN_PASSWD**: `admin`
- **API_KEY**: `dev-api-key-local`

```bash
chmod +x dev-setup.sh
./dev-setup.sh
```

### Using Custom Credentials
To override the default dev credentials, pass them as environment variables before executing the script:

```bash
DB_PASSWORD="MySecureDBPassword" \
ADMIN_PASSWD="MySecureAdminPassword" \
API_KEY="MySecureAPIKey" \
./dev-setup.sh
```

## What the Script Does:
1. Validates that Docker is installed and reachable.
2. Installs K3s locally utilizing the official installation script.
3. Builds the `saas-portal:dev` container image locally and loads it into the K3s containerd registry.
4. Generates development secrets and deploys PostgreSQL, Traefik routes, and RBAC permissions.
5. Deploys the main `odoo-admin` instance and applies hot-patches to use your custom database passwords smoothly.
6. Maps the active NodePorts for immediate local access via output links.

## Output
When finished, the script will map the API and the Admin Odoo instances to your local WSL network IP and provide you with instant access links printed in the console.
