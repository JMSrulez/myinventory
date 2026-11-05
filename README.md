# Inventaire Meraki

Collecteur local pour parcourir les organisations et réseaux du Dashboard Meraki.

## Déploiement Docker sur Ubuntu

Le conteneur écoute sur le réseau Docker. Par défaut, le port `8086` est publié uniquement sur `127.0.0.1` de l’hôte, et l’interface est accessible depuis Ubuntu sur `http://127.0.0.1:8086`.

### Configuration avec .env

Cette méthode convient à une installation Docker Compose sur un hôte maîtrisé. Docker transmet la variable au conteneur, où elle est disponible dans son environnement. Les administrateurs Docker peuvent donc la voir avec les outils d’inspection.

1. Copiez `.env.example` vers `.env`.
2. Renseignez `MERAKI_API_KEY` et, si nécessaire, ajustez `PUBLISHED_PORT` (port de l’hôte) et `MERAKI_INVENTORY_PORT` (port d’écoute dans le conteneur).
3. Restreignez l’accès au fichier : `chmod 600 .env`.
4. Démarrez l’application avec `docker compose up -d --build`.
5. Ouvrez `http://127.0.0.1:8086`.

Le fichier `.env` est exclu de Git. La clé n’est pas incluse dans l’image Docker. Elle est lue au démarrage et n’est jamais renvoyée à la page web.

### Configuration avec un fichier secret

Pour éviter de transmettre la clé comme variable d’environnement du conteneur, Compose peut monter un fichier secret en lecture seule. La clé reste toutefois à protéger sur le disque Ubuntu.

Créez le fichier avec des permissions lisibles par l’utilisateur non privilégié du conteneur :

    sudo mkdir -p secrets
    sudo install -o root -g 10001 -m 0440 /dev/null secrets/meraki_api_key.txt
    sudoedit secrets/meraki_api_key.txt

Lancez ensuite avec les deux fichiers Compose :

    docker compose -f compose.yaml -f compose.secret.yaml up -d --build

Le fichier secret est exclu du contexte Docker et de Git. Pour ce mode, la valeur `MERAKI_API_KEY` de `.env` est ignorée.

### Accès depuis un autre poste

La publication sur `127.0.0.1` protège l’accès par défaut. Pour utiliser un nom d’hôte ou un proxy inverse, adaptez `PUBLISHED_BIND_IP` et `MERAKI_INVENTORY_ALLOWED_HOSTS` dans `.env`. N’exposez pas directement l’application sur Internet sans ajouter une authentification au proxy.

## Démarrage sans Docker

Python 3.10 ou plus récent est requis. Aucun paquet tiers n’est nécessaire. Lancez `python app.py` puis ouvrez `http://127.0.0.1:8086`. Sans clé configurée dans l’environnement, l’interface permet de la saisir ; elle reste en mémoire jusqu’à l’arrêt du serveur ou la déconnexion.

## Phase 1

- connexion à l’API Dashboard Meraki;
- affichage et sélection de plusieurs organisations;
- chargement et sélection des réseaux correspondants.

La collecte des équipements et le stockage local des données d’inventaire sont prévus dans les phases suivantes.
