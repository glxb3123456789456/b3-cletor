"""Ponto de entrada do coletor. Rodado pelo GitHub Actions (ou manualmente)."""
from collector.core import run
from collector.registry import FONTES

if __name__ == "__main__":
    run(FONTES)
