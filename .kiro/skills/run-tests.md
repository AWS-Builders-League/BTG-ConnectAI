# Skill: Ejecutar Tests del Proyecto

## Descripción
Ejecuta los tests del proyecto (unitarios, property-based o todos) y reporta resultados.

## Pasos

1. Ejecutar `pytest src/tests/ -v --tb=short` desde la raíz del proyecto
2. Si hay fallos, analizar el traceback de cada test fallido
3. Sugerir correcciones para los tests que fallan
4. Reportar cobertura si se pide: `pytest src/tests/ --cov=src/lambdas --cov=src/shared --cov-report=term-missing`

## Variantes

- Solo unitarios: `pytest src/tests/unit/ -v`
- Solo property-based: `pytest src/tests/property/ -v`
- Solo integración: `pytest src/tests/integration/ -v`
- Un módulo específico: `pytest src/tests/unit/test_<nombre>.py -v`
