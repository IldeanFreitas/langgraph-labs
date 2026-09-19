"""Os labs não são pacotes: cada pasta entra no sys.path para o pytest importar
`roteador`, `mesa` e `servico` direto. O lab 3 fica de fora - precisa de
Postgres, e o CI o exercita pela CLI (ver .github/workflows/ci.yml)."""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
for lab in ("lab02", "lab04", "lab05"):
    sys.path.insert(0, str(RAIZ / lab))
