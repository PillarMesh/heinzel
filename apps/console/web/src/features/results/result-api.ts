import type {AnswerResultPageView, AnswerResultColumnView, AnswerResultCell} from "../../api/generated"
export type ResultView = AnswerResultPageView
export type ResultColumn = AnswerResultColumnView
export type ResultValue = AnswerResultCell
export interface ResultClient {
  getResult(requestId: string, cursor?: string, pageSize?: number): Promise<ResultView>
}
