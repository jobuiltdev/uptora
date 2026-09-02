import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ACCESS_COOKIE } from "@/lib/auth";

export default async function Home() {
  const jar = await cookies();
  redirect(jar.has(ACCESS_COOKIE) ? "/dashboard" : "/login");
}
