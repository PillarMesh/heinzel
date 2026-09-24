// Types for the precompiled validators emitted by scripts/generate-contracts.mjs.
import type {ValidateFunction} from "ajv"

declare const validators: Record<string, ValidateFunction>
export = validators
