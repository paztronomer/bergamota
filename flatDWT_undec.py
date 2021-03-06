"""flatDWT
Created: September 29, 2016

This script must be able to detect if a flat is acceptable inmediatly after
exposure
Use dflats tagged as bad in FLAT_QA
Use cropped pieces of code from flatStat and decam_test

Oct 4th: DMWY as selected wavelet (symmetric,orthogonal,biorthogonal)

May 30th, 2017: change from decimated to undecimated discrete wavelet
"""
import os
import sys
import time
import socket
import gc
import pickle
import logging
import numpy as np
import scipy.stats
import scipy.signal
import scipy.interpolate
import matplotlib.pyplot as plt
import fitsio
import pywt
import tables


class Toolbox():
    @classmethod
    def dbquery(cls,toquery,outdtype,dbsection='db-desoper',help_txt=False):
        """the personal setup file .desservices.ini must be pointed by desfile
        DB section by default will be desoper
        """
        import despydb.desdbi as desdbi
        desfile = os.path.join(os.getenv("HOME"),".desservices.ini")
        section = dbsection
        dbi = desdbi.DesDbi(desfile,section)
        if help_txt: help(dbi)
        cursor = dbi.cursor()
        cursor.execute(toquery)
        cols = [line[0].lower() for line in cursor.description]
        rows = cursor.fetchall()
        outtab = np.rec.array(rows,dtype=zip(cols,outdtype))
        return outtab

    @classmethod
    def q_one(cls,fnm_compare):
        q = "select factor,rms,worst"
        q +=" from flat_qa"
        q += " where filename='{0}'".format(fnm_compare)
        datatype = ["f4","f4","f4"]
        tab1 = Toolbox.dbquery(q,datatype)
        return tab1

    @classmethod
    def q_two(cls,fnm_any):
        q = "select band,nite,expnum,pfw_attempt_id"
        q += " from miscfile"
        q += " where filename='{0}'".format(fnm_any)
        dt = ["a10","i4","i4","i4"]
        tab2 = Toolbox.dbquery(q,dt)
        return tab2

    @classmethod
    def range_str(cls,head_rng):
        head_rng = head_rng.strip("[").strip("]").replace(":",",").split(",")
        return map(lambda x: int(x)-1, head_rng)

    @classmethod
    def check_folder(cls,folder):
        """Method to check for the folder existence, if not present, tries to
        create it
        Inputs
        - folder path
        """
        if not os.path.exists(folder):
            try:
                os.makedirs(folder)
                logging.info("Creating directory {0}".format(folder))
            except:
                logging.error("Issue creating the folder {0}".format(folder))
        else:
            logging.info("Checking: directory {0} exists".format(folder))
        return True

    @classmethod
    def split_path(cls,path):
        """Method to return the relative root folder (one level upper),
        given a path.
        Inputs
        - path: complete path
        Returns
        - 2 strings, one being the parent folder, and the filename
        """
        #relat_root = os.path.abspath(os.path.join(path,os.pardir))
        relroot,filename = os.path.split(path)
        return relroot,filename

    @classmethod
    def quick_stat(cls,arr_like):
        """Method to make quick stats of an array. It only prints the results,
        dont give a catchable output
        Inputs
        - array
        """
        MAD = np.median( np.abs(arr_like-np.median(arr_like)) )
        print "_"*30
        print "* Min | Max | Mean = {0} | {1} | {2}".format(
            np.min(arr_like),np.max(arr_like),np.mean(arr_like))
        print "* Median | Std | MAD = {0} | {1} | {2}".format(
            np.median(arr_like),np.std(arr_like),MAD)
        print "* .25 | .5 | .75 = {0} | {1} | {2}".format(
            np.percentile(arr_like,.25),np.percentile(arr_like,.5),
            np.percentile(arr_like,.75))
        return False


class FPBinned():
    def __init__(self,fpath):
        """Simple method to open focal plane binned images
        When a position not belongs to focal plane, the value is -1
        Before return it, add 1 to set outer region to zero value
        Inputs
        - fpath: complet path to file
        """
        M_header = fitsio.read_header(fpath)
        M_hdu = fitsio.FITS(fpath)[0]
        tmp = M_hdu.read()
        tmp += 1.
        self.fpBinned = tmp


class DWT():
    @classmethod
    def onelevel(cls,img_arr,wvfunction="dmey",wvmode="zero"):
        """Wavelet decomposition in 1 level
        Inputs
        - img_arr: 2D array containing image data
        - wvfunction: mother wavelet to be used
        - wvmode: method to deal with borders
        Output
        - (c_A,(c_H,c_V,c_D))
        Coeffs:
        c_A : approximation (mean of coeffs) coefs
        c_H,c_V,c_D : horizontal detail,vertical,and diagonal coeffs
        """
        c_onel = pywt.dwt2(img_arr,pywt.Wavelet(wvfunction),wvmode)
        #rec_img = pywt.idwt2((c_A,(c_H,c_V,c_D)),WVstr)#,mode="sym")
        return c_onel

    @classmethod
    def dec_mlevel(cls,img_arr,wvfunction="dmey",wvmode="zero",lev_end=8):
        """Wavelet Decomposition in multiple levels (decimated), differs to
        DWT which is the wavelet transform of one level only
        Inputs
        - img_arr: 2D array containing image data
        - wvfunction: mother wavelet to be used
        - wvmode: method to deal with borders
        - Nlev: number of level for decomposition (max is 8)
        Outputs
        - WAVEDEC2 output is a tuple (cA, (cH, cV, cD)) where (cH, cV, cD)
          repeats Nwv times
        """
        while True:
            try:
                c_ml = pywt.wavedec2(img_arr,pywt.Wavelet(wvfunction),
                                    wvmode,level=lev_end)
                break
            except:
                Nlev -= 1
        return c_ml

    @classmethod
    def undec_mlevel(cls,img_arr,wvfunction="dmey",lev_end=8):
        """Stationary wavelet decomposition in 2D. Undecimated.
        Inputs
        - img_arr: 2D array
        - wvfunction: mother wavelet, string
        - lev_end: final level of decomposition. Maximum is 8
        - lev_ini: initial level of decomposition. Minumum is 0
        Outputs
        - list of tuples (cA, (cH, cV, cD)), one per decomposition level
        """
        while True:
            try:
                c_ml = pywt.swt2(img_arr,pywt.Wavelet(wvfunction),
                                lev_end,axes=(0,1))
                break
            except:
                lev_end -= 1
        return c_ml


class Coeff(DWT):
    @classmethod
    def set_table(cls,dwt_res,outname=None,guess_rows=8,
                table_name="table",title=""):
        """Method for initialize the file to be filled with the results from
        the DWT decomposition.
        Descriptions for pytables taken from "Numerical Python: A practical
        techniques approach for Industry"
        """
        if table_name is None:
            table_name = "pid{0}.table".format(os.getpid())
        #create a new pytable HDF5 file handle. This does not represents the
        #root group. To access the root node must use cls.h5file.root
        cls.h5file = tables.open_file(
            outname,
            mode="w",
            title="HDF5 table for DWT",
            driver="H5FD_CORE")
        #create groups of the file handle object. Args are: path to the parent
        #group (/), the group name, and optionally a title for the group. The
        #last is a descriptive attribute can be set to the group.
        group = cls.h5file.create_group(
            "/",
            "dflat",
            title="Dome Flat Analysis")
        #the file handle is defined, also the group inside it. Under the group
        #we will save the DWT tables.

        #to create a table with mixed type structure we create a class that
        #inherits from tables.IsDescription class, or we can create from a
        #dictionary, ndarray, among others.
        #See http://www.pytables.org/usersguide/libref/
        #file_class.html#tables.File.create_table

        #Will use a dictionary, to setup columns in native Col()
        #Mind the structure: [(cA,(cH,cV,cD)),(cA,(cH,cV,cD)),...]
        diccio = dict()
        for i in xrange(len(dwt_res)):
            coeff = "lev{0}".format(i+1)
            diccio[coeff] = tables.FloatCol(shape=dwt_res[i][0].shape)

        #with the table structure already defined, the table with DWT results
        #can be fully created. Args are: a group object or the path to the root
        #node, the table name, the table structure specification, and
        #optionally the table title. The last is stored as attribute.
        cls.cml_table = cls.h5file.create_table(
            group,
            table_name,
            description=diccio,
            title=title,
            expectedrows=guess_rows)
        #cls.h5file.flush()
        #https://groups.google.com/forum/#!topic/pytables-users/EqxD5zHroc8

    @classmethod
    def fill_table(cls,dwt_res,info_table={}):
        """Method to fill the HDF5 file with the DWT results
        Inputs:
        - dwt_res: results from DWT
        - info_table: information in form of a dictionary, to be filled as
        table attribute
        """
        #using .attrs or ._v_attrs we can access different levels in the
        #HDF5 structure
        cls.cml_table.attrs.DB_INFO = info_table
        cml_row = Coeff.cml_table.row
        try:
            #dwt_res has structure:[(cA,(cH,cV,cD)),...,(cA,(cH,cV,cD))]
            reord = [(a,h,v,d) for (a,(h,v,d)) in dwt_res]
            reord = zip(*reord)
            dwt_res = None
            #with zip, list is now: [(cA,cA,cA,...),(cH,cH,cH,...),...]
            for cx in reord:
                for idx,L in enumerate(Coeff.cml_table.colnames):
                    cml_row[L] = cx[idx]
                cml_row.append()
                Coeff.cml_table.flush()
        except:
            logging.error("Error filling up the table")

    @classmethod
    def close_table(cls):
        Coeff.cml_table.flush()
        Coeff.h5file.close()


class Caller(Coeff):
    """Class inherites from Coeff, which inherites from DWT
    """
    def __init__(self,fname,wvmother=None,wvmethod=None,declevel=None):
        """Method acting as wrapper of the calling of DWT and the filling the
        output tables
        Inputs
        - fname: filename of the 2D array on which DWT will be performed
        - wvmother: string corresponfing to the mother wavelet to be used
        - wvmethod: wheter to use one decomposition level, decimated or
        undecimated. Strings can be "one", "dec", "und"
        - declevel: for the multilevel DWT decomposition, when selecting
        "dec" or "und"
        """
        gc.collect()
        t0 = time.time()
        root,npyfile = Toolbox.split_path(fname)
        info = "\nWorking on {0}. {1}".format(npyfile,time.ctime())
        info += "\n\t(*)Wavelet: {0}".format(wvmother)
        print info
        logging.info(info)
        #load the binned focal plane
        if False:
            bin_fp = np.load(fname)
        else:
            bin_fp = FPBinned(fname).fpBinned
        #for each mother wavelet, save in a different folder
        out_folder = os.path.join(root,wvmother)
        #check if the folder exists, if not, create it
        Toolbox.check_folder(out_folder)
        #setup the output name
        out_name = "{0}_{1}.h5".format(wvmother,npyfile[:npyfile.find(".fits")])
        out_name = os.path.join(out_folder,out_name)
        #perform the DWT
        res = DWT.undec_mlevel(bin_fp,wvfunction=wvmother,lev_end=declevel)
        #setup the table to harbor results
        Coeff.set_table(res,
                    outname=out_name,
                    table_name=wvmother,
                    title="DWT type: {0}".format(wvmother))
        #fill the table
        """HERE: I need to add basic exposure information like: filter, expnum,
        pfw_attempt_id, nite. The attribute is accesible by:
        <H5table object>.attrs.DB_INFO
        """
        d = dict()
        d["module"] = "PyWavelets v{0}".format(pywt.__version__)
        d["wavelet"] = wvmother
        d["time"] = time.ctime()
        ###patch code: use it as base for final version
        c1 = npyfile.find("pixcor-dflat-binned-fp")
        if c1 >= 0:
            tmp1 = Toolbox.q_one(npyfile.replace("pixcor","compare"))
        else:
            tmp1 = Toolbox.q_one(npyfile)
        tmp2 = Toolbox.q_two(npyfile)
        d["factor"] = tmp1["factor"][0]
        d["rms"] = tmp1["rms"][0]
        d["worst"] = tmp1["worst"][0]
        d["band"] = tmp2["band"][0]
        d["nite"] = tmp2["nite"][0]
        d["expnum"] = tmp2["expnum"][0]
        d["pfw_attempt_id"] = tmp2["pfw_attempt_id"][0]
        ###patch code
        Coeff.fill_table(res,info_table=d)
        #close the file
        Coeff.close_table()
        t1 = time.time()
        info_end = "Ended with {0}. Elapsed: {1:.3f} sec".format(npyfile,t1-t0)
        print info_end
        logging.info(info_end)


if __name__=="__main__":
    #directory setup
    #campus cluster precal nodes or macbook
    if socket.gethostname() in ["ccc0027","ccc0028","ccc0029","ccc0030"]:
        npy_folder = "scratch/binned_fp/pixcor_binfp"
    else:
        pass

    path = os.path.join(os.path.expanduser("~"),npy_folder)
    fn = lambda s: os.path.join(path,s)
    #wavelet library setup
    #wvmother = ["dmey","morl","cmor","shan","gaus","mexh","haar"]
    wvmother = ["dmey","haar"]
    wv_level = 3
    #to retrict the depth to be walked!
    DEPTH = 0
    for root,dirs,files in os.walk(path):
        if root.count(os.sep) >= DEPTH:
            del dirs[:]
        FPname = [fn(binned) for binned in files if ((".fits" in binned) and
                (".npy" not in binned))]
        """below can be replaced by a map()"""
        for fn in FPname:
            for wv_n in wvmother:
                if len(pywt.wavelist(wv_n)) == 1:
                    doit = Caller(fn,wvmother=wv_n,declevel=wv_level)
                elif len(pywt.wavelist(wv_n)) > 1:
                    for wv_sub in pywt.wavelist(wv_n):
                        doit = Caller(fn,wvmother=wv_sub,declevel=wv_level)
