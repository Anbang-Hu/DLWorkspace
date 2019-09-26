import React from "react";
import {
  CircularProgress, createMuiTheme, Grid,
  MuiThemeProvider, Paper, SvgIcon,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow, Typography, useTheme
} from "@material-ui/core";
import Iframe from "react-iframe";
import useCheckIsDesktop from "../../../utlities/layoutUtlities";
import {checkObjIsEmpty} from "../../../utlities/ObjUtlities";
import ServicesChips from "./ServicesChips";
import {red} from "@material-ui/core/colors";
import Tooltip from "@material-ui/core/Tooltip";
import IconButton from "@material-ui/core/IconButton";

interface PhClusterNSType {
  nodeStatus: any;
}
const tableTheme = createMuiTheme({
  overrides: {
    MuiTableCell: {
      root: {
        paddingTop: 4,
        paddingBottom: 4,
        paddingLeft:2,
        paddingRight:4,
      }
    }
  }
});
export const PhysicalClusterNodeStatus = (props: PhClusterNSType) => {
  const theme = useTheme();
  const {nodeStatus} = props;
  return (
    <MuiThemeProvider theme={useCheckIsDesktop ? theme : tableTheme}>
      <Table size={ 'small'} >
        <TableHead>
          <TableRow>
            <TableCell>Node Name</TableCell>
            <TableCell>Node IP</TableCell>
            <TableCell>GPU</TableCell>
            <TableCell>Used</TableCell>
            <TableCell>Available</TableCell>
            <TableCell>Status</TableCell>
            {/*<TableCell>Services</TableCell>*/}
            <TableCell>Pods</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {
            nodeStatus.map((ns: any) => {
              const gpuCap = checkObjIsEmpty(ns['gpu_capacity']) ? 0 :  (Number)(Object.values(ns['gpu_capacity'])[0]);
              const gpuUsed = checkObjIsEmpty(ns['gpu_used']) ? 0 : (Number)(Object.values(ns['gpu_used'])[0]);
              const availableGPU = gpuCap - gpuUsed;
              const status = ns['unschedulable'] ? "unschedulable" : "ok";
              let services: string[] = [];
              for (let service of ns['scheduled_service']) {
                services.push(`${service}`);
              }
              return  (
                <TableRow key={ns['name']}>
                  <TableCell style={{ width:'20%' }}>{ns['name']}</TableCell>
                  <TableCell>{ns['InternalIP']}</TableCell>
                  <TableCell>{gpuCap}</TableCell>
                  <TableCell>{gpuUsed}</TableCell>
                  <TableCell>{availableGPU}</TableCell>
                  <TableCell>{status === 'ok' ?
                    <Tooltip title="ok">
                      <IconButton color="primary" size="small">
                        <SvgIcon>
                          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path fill="none" d="M0 0h24v24H0z"/><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2z"/></svg>
                        </SvgIcon>
                      </IconButton>
                    </Tooltip> : <Tooltip title="View Cluster GPU Status Per Node">
                      <IconButton color="secondary"  size="small">
                        <SvgIcon>
                          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path clip-rule="evenodd" fill="none" d="M0 0h24v24H0z"/><path d="M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1.1-1.4z"/></svg>
                        </SvgIcon>
                      </IconButton>
                    </Tooltip>}</TableCell>
                  {/*<TableCell>*/}
                  {/*  {*/}
                  {/*    <ServicesChips services={services}/>*/}
                  {/*  }*/}
                  {/*</TableCell>*/}
                  <TableCell>
                    {
                      ns['pods'].map((pod: string)=>{
                        if (!pod.includes("!!!!!!")) {
                          return (
                            <>
                              <Typography variant="subtitle2" component="p" gutterBottom>
                                <strong>{`[${pod}]`}</strong>
                              </Typography>
                              <br/>
                            </>
                          )
                        } else {
                          return (
                            <>
                              <Typography variant="subtitle2" component="p" style={{ color:red[400] }} gutterBottom>
                                <strong>{`[${pod.replace("!!!!!!", "")}]`}</strong>
                              </Typography>
                              <br/>
                            </>
                          )
                        }
                      })
                    }
                  </TableCell>
                </TableRow>
              )
            })
          }
        </TableBody>
      </Table>
    </MuiThemeProvider>
  )
}
