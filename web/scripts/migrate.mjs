// Creates the tables. Usage: DATABASE_URL=... node scripts/migrate.mjs
import { readFileSync } from "node:fs";
import { neon } from "@neondatabase/serverless";
import { loadEnv } from "./env.mjs";

loadEnv();
if (!process.env.DATABASE_URL) {
  console.error("DATABASE_URL is not set (put it in web/.env).");
  process.exit(1);
}
const sql = neon(process.env.DATABASE_URL);
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf8");
for (const stmt of schema.split(";").map((s) => s.trim()).filter(Boolean)) {
  await sql.query(stmt);
}
console.log("Schema ready.");
