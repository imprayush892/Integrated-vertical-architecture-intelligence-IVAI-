import { describe, expect, it } from "vitest";
import { boundsOf, centroid, fitView, toPlan, toScreen } from "./geom";

describe("plan <-> screen transforms", () => {
  it("fits bounds inside the canvas with y flipped", () => {
    const b = boundsOf([[[0, 0], [20, 0], [20, 10], [0, 10]]]);
    const v = fitView(b, 840, 480, 20);
    const [x0, y0] = toScreen(v, [0, 0]);
    const [x1, y1] = toScreen(v, [20, 10]);
    expect(x0).toBeGreaterThanOrEqual(19.9);
    expect(x1).toBeLessThanOrEqual(820.1);
    expect(y0).toBeGreaterThan(y1); // plan y up = screen y down
    expect(y1).toBeGreaterThanOrEqual(19.9);
  });
  it("round-trips points", () => {
    const v = fitView({ minx: -5, miny: -3, maxx: 9, maxy: 4 }, 500, 400);
    const p: [number, number] = [3.25, -1.5];
    const q = toPlan(v, toScreen(v, p));
    expect(q[0]).toBeCloseTo(p[0], 9);
    expect(q[1]).toBeCloseTo(p[1], 9);
  });
  it("finds polygon centroids", () => {
    const c = centroid([[0, 0], [4, 0], [4, 2], [0, 2]]);
    expect(c[0]).toBeCloseTo(2);
    expect(c[1]).toBeCloseTo(1);
  });
});
