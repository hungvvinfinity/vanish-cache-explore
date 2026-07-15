# Native-service deployment

This Ansible deployment targets one x86_64 Ubuntu 24.04 host. It runs the
same read-only HTTPS S3 gateway as the Compose demo, but as native systemd
services: Nginx on HTTPS, Varnish on loopback, and SeaweedFS on loopback.

## Configure and deploy

1. Copy `inventory/hosts.yml.example` to `inventory/hosts.yml` and replace the
   example host, hostname, TLS paths, and SeaweedFS archive SHA-256.
2. Ensure the certificate and private key already exist on the target and the
   certificate covers `gateway_public_hostname`.
3. From this directory, run:

   ```bash
   ansible-playbook site.yml
   ansible-playbook verify.yml
   ```

The role deliberately does not manage UFW or any other host firewall. Permit
TCP/443 through the network policy separately. SeaweedFS and Varnish listen
only on `127.0.0.1`; Varnish PURGE is therefore available only from the host.

`gateway_seaweedfs_release_sha256` is mandatory. Obtain it from the official
SeaweedFS 4.39 release artifact before deployment; do not replace it with an
unchecked download.
