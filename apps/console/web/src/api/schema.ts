import type {ErrorObject, ValidateFunction} from "ajv"

import consoleApiSchema from "../../../schema/console-api-v1.json"
import * as generatedValidators from "./generated-validators.js"
import type {ConsoleApiSchema} from "./generated"

type ResponseName = {
  [Name in keyof ConsoleApiSchema]: Name extends `${string}_response` ? Name : never
}[keyof ConsoleApiSchema]

type SchemaResponseName = Extract<
  keyof typeof consoleApiSchema.properties,
  `${string}_response`
>
type ResponseNamesMatch =
  [ResponseName] extends [SchemaResponseName]
    ? [SchemaResponseName] extends [ResponseName]
      ? true
      : never
    : never

const responseNamesMatch: ResponseNamesMatch = true
void responseNamesMatch

function isResponseName(name: string): name is ResponseName {
  return name.endsWith("_response")
}

export const modeledResponseNames = Object.keys(consoleApiSchema.properties)
  .filter(isResponseName)
  .sort()

// The validators are precompiled by scripts/generate-contracts.mjs. Ajv builds
// them with `new Function`, which the console's Content-Security-Policy forbids,
// so compiling at runtime would leave the served bundle throwing before React
// mounted. The drift check keeps the generated file honest against the schema.
const validators = new Map<ResponseName, ValidateFunction>(
  modeledResponseNames.map((responseName) => [
    responseName,
    generatedValidators[responseName] as ValidateFunction,
  ]),
)


function validatorFor(responseName: ResponseName): ValidateFunction {
  const validator = validators.get(responseName)
  if (validator === undefined) {
    throw new Error(`Missing runtime validator for ${responseName}`)
  }
  return validator
}

export class ConsoleSchemaValidationError extends Error {
  readonly responseName: ResponseName
  readonly validationErrors: readonly ErrorObject[]

  constructor(responseName: ResponseName, validationErrors: readonly ErrorObject[]) {
    super(`Malformed ${responseName.replaceAll("_", " ")}`)
    this.name = "ConsoleSchemaValidationError"
    this.responseName = responseName
    this.validationErrors = validationErrors
  }
}

function isModeledResponse<Name extends ResponseName>(
  responseName: Name,
  payload: unknown,
): payload is ConsoleApiSchema[Name] {
  return validatorFor(responseName)(payload) === true
}

export function validateConsoleResponse<Name extends ResponseName>(
  responseName: Name,
  payload: unknown,
): ConsoleApiSchema[Name] {
  if (!isModeledResponse(responseName, payload)) {
    throw new ConsoleSchemaValidationError(responseName, validatorFor(responseName).errors ?? [])
  }
  return payload
}
