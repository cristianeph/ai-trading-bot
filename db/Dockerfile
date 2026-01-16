# db/Dockerfile
FROM mysql:8.4

# Opcional: configuración custom
COPY my.cnf /etc/mysql/conf.d/my.cnf

# Opcional: scripts de inicialización (solo corren si /var/lib/mysql está vacío)
COPY init/ /docker-entrypoint-initdb.d/