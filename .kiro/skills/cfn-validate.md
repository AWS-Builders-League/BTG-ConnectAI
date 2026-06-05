# Skill: Validar Templates CloudFormation

## Descripción
Valida todos los templates CloudFormation del proyecto usando cfn-lint y reporta errores.

## Pasos

1. Ejecutar `cfn-lint cloudformation/**/*.yaml` desde la raíz del proyecto
2. Si hay errores, listar cada uno con archivo, línea y descripción
3. Sugerir correcciones para cada error encontrado
4. Ejecutar `checkov -d cloudformation/ --quiet` para validaciones de seguridad adicionales
