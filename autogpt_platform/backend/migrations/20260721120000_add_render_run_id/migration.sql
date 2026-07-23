-- Persist the Render Workflows task run id on each graph execution so that a
-- later cancellation (stop_graph_execution) can cancel the corresponding run
-- via the Render SDK. Null on the RabbitMQ path and before dispatch.
--
-- Authored by Stream C. NOT applied here: `prisma migrate deploy` is owned by
-- exactly one service's predeploy (rest_server, Stream E) to avoid racing
-- concurrent deploys. Additive + nullable, so it is safe under the resume /
-- retry model and requires no backfill.
ALTER TABLE "AgentGraphExecution" ADD COLUMN "renderRunId" TEXT;
