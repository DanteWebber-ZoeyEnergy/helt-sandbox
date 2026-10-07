// AppSync Events API `helt-live` (us-east-1), channel namespace `packs`: the
// live push for INTERNAL dashboard viewers (HANDOFF.md 2026-10-07, "H").
//
// Channels: /packs/<pack_id>, published by the shared ingest Lambda
// (lambdas/ingest publish_live, IAM: inline policy helt-live-publish on
// helt-lambda-role + sandbox-lambda-role = appsync:EventPublish on this
// namespace only). Subscribing needs a Cognito token of pool helt-users
// (dashboard app client) AND this handler's check: the caller's '*' row in
// helt_entitlements must hold ALL (internal; helt-ops today). A customer's
// subscribe is refused ("Unauthorized"); a failed lookup is an error the
// dashboard retries, never a refusal it keeps.
//
// Data source `entitlements` = table helt_entitlements via role
// helt-live-appsync-ddb (dynamodb:GetItem on that table only). Deployed with
//   aws appsync create-channel-namespace --api-id <id> --name packs \
//     --code-handlers file://aws/live_packs_handler.js \
//     --handler-configs '{"onSubscribe":{"behavior":"CODE","integration":{"dataSourceName":"entitlements"}}}'
// (update-channel-namespace with the same flags to change it).
//
// Method shorthand is required: with a data source, the `{request: fn}` form
// fails live with HandlerExecutionError -- although `aws appsync
// evaluate-code` (the resolver runtime) accepts only that form.
import { util } from '@aws-appsync/utils'
import * as ddb from '@aws-appsync/utils/dynamodb'

export const onSubscribe = {
  request(ctx) {
    return ddb.get({ key: { user_id: ctx.identity.sub, pack_id: '*' } })
  },
  response(ctx) {
    if (ctx.error) {
      util.error('entitlement lookup failed')
    }
    const groups = (ctx.result && ctx.result.field_groups) || []
    if (groups.indexOf('ALL') < 0) {
      util.unauthorized()
    }
  },
}
