"""Carrega configuração do AWS Systems Manager Parameter Store.

Uso típico em OKD/OpenShift:
  env SSM_PREFIX=/resumeai/prod
  env AWS_REGION=us-east-1
  + credenciais AWS via ServiceAccount/Secret ou AWS_ACCESS_KEY_ID
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("resumeai-bot")


def load_config_from_ssm() -> None:
    """Carrega /prefix/* do SSM para os.environ.

    Ativado somente se SSM_PREFIX estiver definido (ex.: /resumeai/prod).

    Regras:
    - Não sobrescreve variável que já exista no ambiente
      (Secret/ConfigMap do OKD tem prioridade sobre o SSM).
    - SecureString é descriptografado (WithDecryption=True).
    - Nome final = última parte do path
      (/resumeai/prod/SLACK_BOT_TOKEN → SLACK_BOT_TOKEN).

    Credenciais AWS (qualquer uma):
    - IRSA / role anotada no ServiceAccount (recomendado em EKS;
      em OKD use o mecanismo equivalente da sua conta)
    - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN)
    - Arquivo de credenciais montado em volume
    """
    prefix = (os.environ.get("SSM_PREFIX") or "").strip()
    if not prefix:
        return
    if not prefix.startswith("/"):
        prefix = "/" + prefix

    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "SSM_PREFIX definido, mas o pacote boto3 não está instalado. "
            "Inclua boto3 no requirements.txt e reconstrua a imagem."
        ) from e

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    logger.info("SSM: carregando path=%s region=%s", prefix, region)
    client = boto3.client("ssm", region_name=region)
    loaded = []
    try:
        paginator = client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(
            Path=prefix, Recursive=True, WithDecryption=True
        ):
            for param in page.get("Parameters") or []:
                full_name = param.get("Name") or ""
                name = full_name.rstrip("/").split("/")[-1]
                if not name:
                    continue
                # Prioridade: env já injetada no pod (OKD Secret/ConfigMap)
                if os.environ.get(name):
                    continue
                os.environ[name] = param["Value"]
                loaded.append(name)
    except Exception:
        logger.exception("SSM: falha ao ler path=%s", prefix)
        raise

    logger.info(
        "SSM: %d parâmetro(s) aplicado(s): %s",
        len(loaded),
        ", ".join(sorted(loaded)) if loaded else "(nenhum novo)",
    )
