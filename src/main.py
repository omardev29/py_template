"""Punto de entrada. NO se compila con mypyc: solo importa la lógica compilada."""

from app import main

raise SystemExit(main())
