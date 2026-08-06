"""Bootstrap do ResumeAI: carrega SSM (se configurado) e inicia o app.

Usado no container (OKD/Docker) para garantir que os.environ já tenha
os parâmetros do Parameter Store antes do app.py validar REQUIRED.
"""
from __future__ import annotations

import runpy
import sys


def main() -> None:
    # Logging mínimo antes do app configurar o logger próprio
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from ssm_config import load_config_from_ssm

    load_config_from_ssm()

    # Executa app.py no mesmo processo (env já populado)
    sys.argv[0] = "app.py"
    runpy.run_path("app.py", run_name="__main__")


if __name__ == "__main__":
    main()
