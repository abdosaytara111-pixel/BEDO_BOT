import { Router, type IRouter } from "express";
import healthRouter from "./health";
import radioRouter from "./radio";

const router: IRouter = Router();

router.use(healthRouter);
router.use(radioRouter);

export default router;
